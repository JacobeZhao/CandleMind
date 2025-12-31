#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import boto3


class BedrockRuntimeClient:
    def __init__(self):
        self.region = os.getenv("AWS_REGION", "us-west-2")
        self.access_key = os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")

    def get_client(self):
        return boto3.client(
            "bedrock-runtime",
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key
        )


bedrock_runtime_client = BedrockRuntimeClient()
