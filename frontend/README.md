# CandleMind Desktop Application

这是一个使用Electron构建的桌面应用程序，具有桌面监控和AI分析功能。

## 功能特性

- **顶部栏**: 包含Logo和窗口控制按钮（最小化、最大化、关闭）
- **透明监控区域**: 实时监控桌面内容（视觉上呈现为半透明区域）
- **AI分析**: 自动检测屏幕变化并调用AI进行内容分析
- **手动分析**: 用户可以通过文本框手动触发AI分析

## 技术架构

- **Electron**: 构建跨平台桌面应用
- **RobotJS**: 屏幕截图功能
- **Jimp**: 图像处理和分析
- **FastAPI Backend**: AI分析服务接口

## 安装和运行

### 前提条件

- Node.js 14+ 
- Python 3.8+ (用于后端服务)

### 安装步骤

1. 安装前端依赖:
```bash
cd frontend
npm install
```

2. 启动后端服务:
```bash
cd ../backend
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

3. 启动前端应用:
```bash
cd ../frontend
npm start
```

## 使用方法

1. 应用启动后会自动开始每秒截图并保存到内存（最多100张）
2. 当检测到连续的屏幕变化时，会自动触发AI分析
3. 用户也可以在底部文本框输入内容并点击"调用AI分析"按钮手动触发分析
4. 分析结果会显示在输入框下方

## 工作原理

1. **截图机制**: 应用每秒自动截取整个屏幕，并将图像存储在内存缓冲区中
2. **变化检测**: 通过比较连续图像的像素差异来检测屏幕活动
3. **AI分析**: 当检测到足够的变化时，将最近的几张图像发送到后端AI服务进行分析
4. **结果展示**: AI分析结果返回并在界面上显示

## 文件结构

```
frontend/
├── main.js          # Electron主进程
├── renderer.js      # Electron渲染进程
├── index.html       # 主界面
├── package.json     # 项目配置
└── README.md        # 本文档
```

## 注意事项

- 需要授予应用屏幕录制权限（macOS）或类似权限（Windows/Linux）
- 内存中最多保存100张截图以防止内存溢出
- AI分析功能依赖后端服务，请确保后端服务正常运行