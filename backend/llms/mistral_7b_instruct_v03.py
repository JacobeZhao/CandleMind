#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import tiktoken

from llms.bedrock_runtime import bedrock_runtime_client


class Mistral7BInstructV03:
    def __init__(self):
        """
        初始化通用模型服务，只初始化一次模型 client
        """
        self.runtime = bedrock_runtime_client.get_client()
        self.model_id = "arn:aws:bedrock:us-west-2:348152033681:imported-model/uo9y0k8fexk1"
        self.model_name = "mistral-7b-instruct-v0:3"
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

        payload = json.dumps({
            "messages": [{"role": "user", "content": text}],
            "max_tokens": self.max_output_tokens
        })
        response = self.runtime.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=payload
        )
        result = json.loads(response["body"].read())
        # 根据实际返回结构取内容
        return result["choices"][0]["message"]["content"]


if __name__ == '__main__':
    # Example usage
    from env_loader import load_env
    load_env()
    # Initialize the model
    qwen3_32b = Mistral7BInstructV03()
    print(qwen3_32b.chat(text="Hello! Please reply in one sentence."))
