const { app, BrowserWindow, ipcMain, screen } = require('electron');
const path = require('path');
const screenshot = require('screenshot-desktop');
const Jimp = require('jimp');  // 修正Jimp的导入方式

let mainWindow;
let screenshotInterval;
let imageBuffer = []; // 存储最近100张截图
const MAX_BUFFER_SIZE = 100;

// 创建浏览器窗口
function createWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;

  mainWindow = new BrowserWindow({
    width: 800,
    height: 600,
    minWidth: 800,
    minHeight: 600,
    frame: false, // 无边框窗口
    transparent: true, // 透明背景
    backgroundColor: '#00000000',
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
      enableRemoteModule: true
    }
  });

  mainWindow.loadFile('index.html');

  // 开发模式下打开开发者工具
  if (process.argv.includes('--dev')) {
    mainWindow.webContents.openDevTools();
  }

  // 设置截图定时器
  startScreenshotCapture();

  mainWindow.on('closed', () => {
    stopScreenshotCapture();
    mainWindow = null;
  });
}

// 启动截图捕获
function startScreenshotCapture() {
  if (screenshotInterval) {
    clearInterval(screenshotInterval);
  }

  screenshotInterval = setInterval(() => {
    captureScreenshot();
  }, 1000); // 每秒截图一次
}

// 停止截图捕获
function stopScreenshotCapture() {
  if (screenshotInterval) {
    clearInterval(screenshotInterval);
    screenshotInterval = null;
  }
}

// 捕获屏幕截图
async function captureScreenshot() {
  try {
    // 使用 screenshot-desktop 截取整个屏幕
    const imgBuffer = await screenshot();
    
    // 将图像数据转换为JIMP对象以便处理
    const image = await Jimp.read(imgBuffer);

    // 添加时间戳
    image.bitmap.timestamp = Date.now();

    // 添加到缓冲区
    imageBuffer.push(image);

    // 保持缓冲区大小不超过100
    if (imageBuffer.length > MAX_BUFFER_SIZE) {
      imageBuffer.shift();
    }

    console.log(`Screenshot captured: ${image.bitmap.width}x${image.bitmap.height}, buffer size: ${imageBuffer.length}`);
  } catch (error) {
    console.error('Error capturing screenshot:', error);
  }
}

// 检查图像是否发生变化
function hasImageChanged(prevImg, currImg) {
  if (!prevImg || !currImg) return false;
  
  // 计算两张图像之间的差异
  return calculateImageDifference(prevImg, currImg) > 0.05; // 差异阈值设为5%
}

// 计算两张图像之间的差异程度
function calculateImageDifference(img1, img2) {
  // 确保两幅图像尺寸相同
  if (img1.bitmap.width !== img2.bitmap.width || img1.bitmap.height !== img2.bitmap.height) {
    return 1; // 完全不同
  }
  
  // 缩小图像以提高性能
  const sampleWidth = Math.min(img1.bitmap.width, 64);
  const sampleHeight = Math.min(img1.bitmap.height, 64);
  
  const resized1 = img1.clone().resize(sampleWidth, sampleHeight);
  const resized2 = img2.clone().resize(sampleWidth, sampleHeight);
  
  let diffSum = 0;
  let pixelCount = 0;
  
  for (let y = 0; y < sampleHeight; y++) {
    for (let x = 0; x < sampleWidth; x++) {
      const pixel1 = resized1.getPixelColor(x, y);
      const pixel2 = resized2.getPixelColor(x, y);
      
      const rgba1 = Jimp.intToRGBA(pixel1);
      const rgba2 = Jimp.intToRGBA(pixel2);
      
      // 计算RGB差值
      const rDiff = Math.abs(rgba1.r - rgba2.r);
      const gDiff = Math.abs(rgba1.g - rgba2.g);
      const bDiff = Math.abs(rgba1.b - rgba2.b);
      
      // 加权平均差值
      diffSum += (rDiff + gDiff + bDiff) / 3;
      pixelCount++;
    }
  }
  
  // 返回平均差异值（归一化到0-1范围）
  return diffSum / (pixelCount * 255);
}

// 计算图像哈希值（简化版）
function calculateImageHash(image) {
  // 缩小图像以加快处理速度
  const resized = image.clone().resize(16, 16);
  
  // 转换为灰度并获取平均值
  let sum = 0;
  for (let y = 0; y < resized.bitmap.height; y++) {
    for (let x = 0; x < resized.bitmap.width; x++) {
      const pixel = resized.getPixelColor(x, y);
      const rgba = Jimp.intToRGBA(pixel);
      sum += (rgba.r + rgba.g + rgba.b) / 3;
    }
  }
  
  return Math.round(sum / (resized.bitmap.width * resized.bitmap.height));
}

// 获取图像缓冲区
ipcMain.handle('get-image-buffer', async () => {
  return imageBuffer.map(img => ({
    timestamp: img.bitmap.timestamp,
    width: img.bitmap.width,
    height: img.bitmap.height
  }));
});

// 手动触发AI分析
ipcMain.on('trigger-analysis', () => {
  analyzeScreenContent();
});

// 分析屏幕内容
function analyzeScreenContent() {
  if (imageBuffer.length < 2) {
    console.log('Not enough images in buffer for analysis');
    return;
  }

  // 检查最后几张图像是否有连续变化
  let changesDetected = 0;
  const recentImages = imageBuffer.slice(-10); // 检查最新的10张图片
  
  for (let i = 0; i < recentImages.length - 1; i++) {
    if (hasImageChanged(recentImages[i], recentImages[i + 1])) {
      changesDetected++;
    }
  }

  // 如果检测到足够多的变化，则触发AI分析
  if (changesDetected >= 2) { // 至少2次变化才触发分析
    console.log(`Changes detected (${changesDetected}), triggering AI analysis`);
    
    // 使用最新的几张图片进行分析
    const imagesForAnalysis = imageBuffer.slice(-5); // 使用最新的5张图片
    
    // 调用AI分析
    callAIAnalysis(imagesForAnalysis);
  } else {
    console.log(`Insufficient changes detected (${changesDetected}), skipping analysis`);
  }
}

// 调用AI分析服务
async function callAIAnalysis(images) {
  console.log(`Calling AI analysis with ${images.length} images`);
  
  try {
    // 将图像转换为Base64格式以便传输
    const imageBase64List = [];
    
    for (const img of images) {
      // 将JIMP图像转换为Base64
      const buffer = await img.getBufferAsync(Jimp.MIME_PNG);
      const base64Str = buffer.toString('base64');
      imageBase64List.push(base64Str);
    }
    
    // 发送POST请求到后端AI分析API
    const response = await fetch('http://localhost:8000/ai/analyze-screen', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        images: imageBase64List,
        prompt: '请分析这些连续的屏幕截图，识别其中的变化并总结发生了什么。'
      })
    });
    
    if (response.ok) {
      const result = await response.json();
      console.log('AI Analysis Result:', result);
      
      // 发送结果回渲染进程
      mainWindow.webContents.send('analysis-result', result);
    } else {
      const errorText = await response.text();
      console.error('Failed to get AI analysis:', response.status, errorText);
      
      // 发送错误信息到渲染进程
      mainWindow.webContents.send('analysis-result', {
        error: `AI分析失败: ${response.status} - ${errorText}`
      });
    }
  } catch (error) {
    console.error('Error calling AI analysis:', error);
    
    // 发送错误信息到渲染进程
    mainWindow.webContents.send('analysis-result', {
      error: `AI分析出错: ${error.message}`
    });
  }
}

// 应用程序生命周期事件
app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

// IPC处理器 - 控制窗口操作
ipcMain.on('window-minimize', () => {
  mainWindow.minimize();
});

ipcMain.on('window-maximize', () => {
  if (mainWindow.isMaximized()) {
    mainWindow.unmaximize();
  } else {
    mainWindow.maximize();
  }
});

ipcMain.on('window-close', () => {
  mainWindow.close();
});