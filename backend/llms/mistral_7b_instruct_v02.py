#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import tiktoken

from llms.bedrock_runtime import bedrock_runtime_client


class Mistral7BInstructV02:
    def __init__(self):
        """
        初始化通用模型服务，只初始化一次模型 client
        """
        self.runtime = bedrock_runtime_client.get_client()
        self.model_id = "mistral.mistral-7b-instruct-v0:2"
        self.model_name = "mistral-7b-instruct-v0:2"
        self.max_output_tokens = 20480
        self.model_temperature = 0.2
        self.default_response = "Sorry, your input content is too long and cannot be processed. Please shorten it and try again."

    def _count_tokens(self, text):
        """
        计算输入文本的 token 数量
        :param text: 输入文本
        :return: token 数量
        """
        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(text)
        return len(tokens)

    def chat(self, text="Hello! Please reply in one sentence."):
        """
        普通对话方法，输入文本，输出模型回复文本
        :param text: 输入文本
        :return: 输出文本
        """
        if self._count_tokens(text) > self.max_output_tokens:
            return self.default_response

        messages = [{"role": "user", "content": [{"text": text}]}]
        inferenceConfig = {
            "maxTokens": self.max_output_tokens,
            "temperature": self.model_temperature
        }

        response = self.runtime.converse(
            modelId=self.model_id,
            messages=messages,
            inferenceConfig=inferenceConfig
        )

        result = response
        return result["output"]["message"]["content"][0]["text"]


if __name__ == '__main__':
    # Example usage
    from env_loader import load_env
    load_env()
    # Initialize the model
    qwen3_32b = Mistral7BInstructV02()
    print(qwen3_32b.chat(text="Hello! Please reply in one sentence."))
