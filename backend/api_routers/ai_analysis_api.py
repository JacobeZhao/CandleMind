from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Optional
import json
import base64
from datetime import datetime

from backend.services.market_service import MarketDataService
from backend.llms.qwen3_next_80b_a3b import Qwen3Next80BA3BService
from backend.llms.mistral_7b_instruct_v03 import Mistral7BInstructV03Service
from backend.llms.llama31_instruct_8b import Llama31Instruct8BService


ai_analysis_router = APIRouter()
market_service: Optional[MarketDataService] = None
llm_service = Qwen3Next80BA3BService()  # 默认使用Qwen3模型


def init_ai_analysis(market: MarketDataService):
    """
    初始化AI分析服务实例
    
    Args:
        market (MarketDataService): 市场数据服务实例
    """
    global market_service
    market_service = market


@ai_analysis_router.post("/analyze-screen")
async def analyze_screen(
    images: list[str] = Form(...),  # 接收图像数据的Base64编码列表
    prompt: str = Form(None)        # 用户提供的分析提示
):
    """
    分析屏幕截图内容
    
    Args:
        images: Base64编码的图像数据列表
        prompt: 用户提供的分析提示
    """
    if not images:
        raise HTTPException(status_code=400, detail="缺少图像数据")
    
    try:
        # 构建默认提示，如果用户未提供则使用默认值
        if not prompt:
            prompt = "请分析以下连续屏幕截图中的内容变化，并总结发生了什么。"
        
        # 解码图像数据
        decoded_images = []
        for img_base64 in images:
            # 移除data:image前缀（如果有）
            if img_base64.startswith('data:image'):
                img_base64 = img_base64.split(',')[1]
            
            decoded_images.append(img_base64)
        
        # 使用LLM服务分析图像
        analysis_result = await llm_service.analyze_images(decoded_images, prompt)
        
        return {
            "status": "success",
            "analysis": analysis_result,
            "timestamp": datetime.now(),
            "image_count": len(images)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析过程中发生错误: {str(e)}")


@ai_analysis_router.post("/analyze-text")
async def analyze_text(text: str = Form(...), context: str = Form("")):
    """
    分析文本内容
    
    Args:
        text: 要分析的文本
        context: 提供的上下文信息
    """
    if not text:
        raise HTTPException(status_code=400, detail="缺少文本内容")
    
    try:
        # 构建提示词
        prompt = f"请分析以下文本内容：\n\n{text}\n\n上下文：{context if context else '无'}"
        
        # 使用LLM服务分析文本
        analysis_result = await llm_service.generate(prompt)
        
        return {
            "status": "success",
            "analysis": analysis_result,
            "timestamp": datetime.now()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析过程中发生错误: {str(e)}")


@ai_analysis_router.post("/analyze-combined")
async def analyze_combined(
    images: list[str] = Form([]),  # Base64编码的图像数据列表
    text: str = Form(""),          # 要分析的文本
    prompt: str = Form(None)       # 用户提供的分析提示
):
    """
    结合图像和文本进行综合分析
    
    Args:
        images: Base64编码的图像数据列表
        text: 要分析的文本
        prompt: 用户提供的分析提示
    """
    try:
        # 构建综合分析提示
        if not prompt:
            if text and images:
                prompt = f"请结合以下文本和图像进行综合分析：\n\n文本内容：{text}"
            elif text:
                prompt = f"请分析以下文本内容：{text}"
            elif images:
                prompt = "请分析以下图像内容。"
            else:
                raise HTTPException(status_code=400, detail="缺少文本或图像数据")
        
        result = {}
        
        # 如果有图像数据，进行图像分析
        if images:
            decoded_images = []
            for img_base64 in images:
                if img_base64.startswith('data:image'):
                    img_base64 = img_base64.split(',')[1]
                decoded_images.append(img_base64)
            
            image_analysis = await llm_service.analyze_images(decoded_images, prompt)
            result["image_analysis"] = image_analysis
        
        # 如果有文本数据，进行文本分析
        if text:
            text_analysis = await llm_service.generate(f"{prompt}\n\n文本内容：{text}")
            result["text_analysis"] = text_analysis
        
        # 如果两者都有，提供综合分析
        if images and text:
            combined_prompt = f"请基于以下图像和文本内容提供综合分析报告：\n\n图像分析：{image_analysis}\n\n文本分析：{text_analysis}"
            combined_analysis = await llm_service.generate(combined_prompt)
            result["combined_analysis"] = combined_analysis
        elif not result:  # 如果没有图像也没有文本
            raise HTTPException(status_code=400, detail="缺少文本或图像数据")
        
        result["timestamp"] = datetime.now()
        result["status"] = "success"
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析过程中发生错误: {str(e)}")