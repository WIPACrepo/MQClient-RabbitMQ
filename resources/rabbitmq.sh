#!/bin/bash
docker run --rm -it \
    --network=host \
    --hostname my-rabbit \
    --name rabbit \
    deadtrickster/rabbitmq_prometheus:3.7
