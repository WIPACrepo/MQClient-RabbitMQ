"""Back-end using RabbitMQ."""

import logging
import time
from functools import partial
from typing import Any, AsyncGenerator, Callable, Optional, Union

import pika  # type: ignore
from mqclient import backend_interface, log_msgs
from mqclient.backend_interface import (
    RETRY_DELAY,
    TIMEOUT_MILLIS_DEFAULT,
    TRY_ATTEMPTS,
    AlreadyClosedExcpetion,
    ClosingFailedExcpetion,
    Message,
    Pub,
    RawQueue,
    Sub,
)

AMQP_ADDRESS_PREFIX = "amqp://"


class RabbitMQ(RawQueue):
    """Base RabbitMQ wrapper.

    Extends:
        RawQueue
    """

    def __init__(self, address: str, queue: str) -> None:
        super().__init__()
        self.address = address
        if not self.address.startswith(AMQP_ADDRESS_PREFIX):
            self.address = AMQP_ADDRESS_PREFIX + self.address
        self.queue = queue
        self.connection = None  # type: pika.BlockingConnection
        self.channel = None  # type: pika.adapters.blocking_connection.BlockingChannel

    async def connect(self) -> None:
        """Set up connection and channel."""
        await super().connect()
        logging.info(f"Connecting with address={self.address}")
        self.connection = pika.BlockingConnection(
            pika.connection.URLParameters(self.address)
        )
        self.channel = self.connection.channel()

    async def close(self) -> None:
        """Close connection."""
        await super().close()
        if not self.connection:
            raise ClosingFailedExcpetion("No connection to close.")
        if self.connection.is_closed:
            raise AlreadyClosedExcpetion()
        try:
            self.connection.close()
        except Exception as e:
            raise ClosingFailedExcpetion() from e


class RabbitMQPub(RabbitMQ, Pub):
    """Wrapper around queue with delivery-confirm mode in the channel.

    Extends:
        RabbitMQ
        Pub
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        logging.debug(f"{log_msgs.INIT_PUB} ({args}; {kwargs})")
        super().__init__(*args, **kwargs)

    async def connect(self) -> None:
        """Set up connection, channel, and queue.

        Turn on delivery confirmations.
        """
        logging.debug(log_msgs.CONNECTING_PUB)
        await super().connect()

        self.channel.queue_declare(queue=self.queue, durable=False)
        self.channel.confirm_delivery()
        logging.debug(log_msgs.CONNECTED_PUB)

    async def close(self) -> None:
        """Close connection."""
        logging.debug(log_msgs.CLOSING_PUB)
        await super().close()
        logging.debug(log_msgs.CLOSED_PUB)

    async def send_message(self, msg: bytes) -> None:
        """Send a message on a queue.

        Args:
            address (str): address of queue
            name (str): name of queue on address

        Returns:
            RawQueue: queue
        """
        logging.debug(log_msgs.SENDING_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        await try_call(
            self,
            partial(
                self.channel.basic_publish,
                exchange="",
                routing_key=self.queue,
                body=msg,
            ),
        )
        logging.debug(log_msgs.SENT_MESSAGE)


class RabbitMQSub(RabbitMQ, Sub):
    """Wrapper around queue with prefetch-queue QoS.

    Extends:
        RabbitMQ
        Sub
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        logging.debug(f"{log_msgs.INIT_SUB} ({args}; {kwargs})")
        super().__init__(*args, **kwargs)
        self.consumer_id = None
        self.prefetch = 1

    async def connect(self) -> None:
        """Set up connection, channel, and queue.

        Turn on prefetching.
        """
        logging.debug(log_msgs.CONNECTING_SUB)
        await super().connect()
        self.channel.queue_declare(queue=self.queue, durable=False)
        self.channel.basic_qos(prefetch_count=self.prefetch, global_qos=True)
        logging.debug(log_msgs.CONNECTED_SUB)

    async def close(self) -> None:
        """Close connection."""
        logging.debug(log_msgs.CLOSING_SUB)
        await super().close()

        if not self.channel:
            raise ClosingFailedExcpetion("No channel to close.")
        try:
            self.channel.cancel()  # rejects all pending ackable messages
        except Exception as e:
            raise ClosingFailedExcpetion() from e

        logging.debug(log_msgs.CLOSED_SUB)

    @staticmethod
    def _to_message(  # type: ignore[override]  # noqa: F821 # pylint: disable=W0221
        method_frame: Optional[pika.spec.Basic.GetOk], body: Optional[Union[str, bytes]]
    ) -> Optional[Message]:
        """Transform RabbitMQ-Message to Message type."""
        if not method_frame or body is None:
            return None

        if isinstance(body, str):
            return Message(method_frame.delivery_tag, body.encode())
        else:
            return Message(method_frame.delivery_tag, body)

    async def get_message(
        self, timeout_millis: Optional[int] = TIMEOUT_MILLIS_DEFAULT
    ) -> Optional[Message]:
        """Get a message from a queue.

        NOTE - `timeout_millis` is not used.
        """
        logging.debug(log_msgs.GETMSG_RECEIVE_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        method_frame, _, body = await try_call(
            self, partial(self.channel.basic_get, self.queue)
        )
        msg = RabbitMQSub._to_message(method_frame, body)

        if msg:
            logging.debug(f"{log_msgs.GETMSG_RECEIVED_MESSAGE} ({msg.msg_id!r}).")
            return msg
        else:
            logging.debug(log_msgs.GETMSG_NO_MESSAGE)
            return None

    async def ack_message(self, msg: Message) -> None:
        """Ack a message from the queue.

        Note that RabbitMQ acks messages in-order, so acking message
        3 of 3 in-progress messages will ack them all.
        """
        logging.debug(log_msgs.ACKING_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        await try_call(self, partial(self.channel.basic_ack, msg.msg_id))
        logging.debug(f"{log_msgs.ACKED_MESSAGE} ({msg.msg_id!r}).")

    async def reject_message(self, msg: Message) -> None:
        """Reject (nack) a message from the queue.

        Note that RabbitMQ acks messages in-order, so nacking message
        3 of 3 in-progress messages will nack them all.
        """
        logging.debug(log_msgs.NACKING_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        await try_call(self, partial(self.channel.basic_nack, msg.msg_id))
        logging.debug(f"{log_msgs.NACKED_MESSAGE} ({msg.msg_id!r}).")

    async def message_generator(  # type: ignore[override] # there's a mypy bug here
        self, timeout: int = 60, propagate_error: bool = True
    ) -> AsyncGenerator[Optional[Message], None]:
        """Yield Messages.

        Generate messages with variable timeout.
        Yield `None` on `throw()`.

        Keyword Arguments:
            timeout {int} -- timeout in seconds for inactivity (default: {60})
            propagate_error -- should errors from downstream kill the generator? (default: {True})
        """
        logging.debug(log_msgs.MSGGEN_ENTERED)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        msg = None
        try:
            gen = partial(self.channel.consume, self.queue, inactivity_timeout=timeout)

            async for method_frame, _, body in try_yield(self, gen):
                # get message
                msg = RabbitMQSub._to_message(method_frame, body)
                logging.debug(log_msgs.MSGGEN_GET_NEW_MESSAGE)
                if not msg:
                    logging.info(log_msgs.MSGGEN_NO_MESSAGE_LOOK_BACK_IN_QUEUE)
                    break

                # yield message to consumer
                try:
                    logging.debug(f"{log_msgs.MSGGEN_YIELDING_MESSAGE} [{msg}]")
                    yield msg
                # consumer throws Exception...
                except Exception as e:  # pylint: disable=W0703
                    logging.debug(log_msgs.MSGGEN_DOWNSTREAM_ERROR)
                    if propagate_error:
                        logging.debug(log_msgs.MSGGEN_PROPAGATING_ERROR)
                        raise
                    logging.warning(
                        f"{log_msgs.MSGGEN_EXCEPTED_DOWNSTREAM_ERROR} {e}.",
                        exc_info=True,
                    )
                    yield None  # hand back to consumer
                # consumer requests again, aka next()
                else:
                    pass

        # Garbage Collection (or explicit close(), or break in consumer's loop)
        except GeneratorExit:
            logging.debug(log_msgs.MSGGEN_GENERATOR_EXITING)
            logging.debug(log_msgs.MSGGEN_GENERATOR_EXITED)


async def try_call(queue: RabbitMQ, func: Callable[..., Any]) -> Any:
    """Try to call `func` and return value.

    Try up to `TRY_ATTEMPTS` times, for connection-related errors.
    """
    for i in range(TRY_ATTEMPTS):
        if i > 0:
            logging.debug(
                f"{log_msgs.TRYCALL_CONNECTION_ERROR_TRY_AGAIN} (attempt #{i+1})..."
            )

        try:
            return func()
        except pika.exceptions.ConnectionClosedByBroker:
            logging.debug(log_msgs.TRYCALL_CONNECTION_CLOSED_BY_BROKER)
        # Do not recover on channel errors
        except pika.exceptions.AMQPChannelError as err:
            logging.error(f"{log_msgs.TRYCALL_RAISE_AMQP_CHANNEL_ERROR} {err}.")
            raise
        # Recover on all other connection errors
        except pika.exceptions.AMQPConnectionError:
            logging.debug(log_msgs.TRYCALL_AMQP_CONNECTION_ERROR)

        await queue.close()
        time.sleep(RETRY_DELAY)
        await queue.connect()

    logging.debug(log_msgs.TRYCALL_CONNECTION_ERROR_MAX_RETRIES)
    raise Exception("RabbitMQ connection error")


async def try_yield(
    queue: RabbitMQ, func: Callable[..., Any]
) -> AsyncGenerator[Any, None]:
    """Try to call `func` and yield value(s).

    Try up to `TRY_ATTEMPTS` times, for connection-related errors.
    """
    for i in range(TRY_ATTEMPTS):
        if i > 0:
            logging.debug(
                f"{log_msgs.TRYYIELD_CONNECTION_ERROR_TRY_AGAIN} (attempt #{i+1})..."
            )

        try:
            for x in func():  # pylint: disable=invalid-name
                yield x
        except pika.exceptions.ConnectionClosedByBroker:
            logging.debug(log_msgs.TRYYIELD_CONNECTION_CLOSED_BY_BROKER)
        # Do not recover on channel errors
        except pika.exceptions.AMQPChannelError as err:
            logging.error(f"{log_msgs.TRYYIELD_RAISE_AMQP_CHANNEL_ERROR} {err}.")
            raise
        # Recover on all other connection errors
        except pika.exceptions.AMQPConnectionError:
            logging.debug(log_msgs.TRYYIELD_AMQP_CONNECTION_ERROR)

        await queue.close()
        time.sleep(RETRY_DELAY)
        await queue.connect()

    logging.debug(log_msgs.TRYYIELD_CONNECTION_ERROR_MAX_RETRIES)
    raise Exception("RabbitMQ connection error")


class Backend(backend_interface.Backend):
    """RabbitMQ Pub-Sub Backend Factory.

    Extends:
        Backend
    """

    @staticmethod
    async def create_pub_queue(
        address: str, name: str, auth_token: str = ""
    ) -> RabbitMQPub:
        """Create a publishing queue.

        # NOTE - `auth_token` is not used currently

        Args:
            address (str): address of queue
            name (str): name of queue on address

        Returns:
            RawQueue: queue
        """
        q = RabbitMQPub(address, name)  # pylint: disable=invalid-name
        await q.connect()
        return q

    @staticmethod
    async def create_sub_queue(
        address: str, name: str, prefetch: int = 1, auth_token: str = ""
    ) -> RabbitMQSub:
        """Create a subscription queue.

        # NOTE - `auth_token` is not used currently

        Args:
            address (str): address of queue
            name (str): name of queue on address

        Returns:
            RawQueue: queue
        """
        q = RabbitMQSub(address, name)  # pylint: disable=invalid-name
        q.prefetch = prefetch
        await q.connect()
        return q
