const { ipcRenderer, remote } = require('electron');

// DOM元素引用
const minBtn = document.getElementById('minBtn');
const maxBtn = document.getElementById('maxBtn');
const closeBtn = document.getElementById('closeBtn');
const textInput = document.getElementById('textInput');
const analyzeBtn = document.getElementById('analyzeBtn');
const statusText = document.getElementById('statusText');
const analysisResult = document.getElementById('analysisResult');
const resultContent = document.getElementById('resultContent');
const monitorStatus = document.getElementById('monitorStatus');
const statusIndicator = document.getElementById('statusIndicator');

// 绑定窗口控制按钮事件
minBtn.addEventListener('click', () => {
  ipcRenderer.send('window-minimize');
});

maxBtn.addEventListener('click', () => {
  ipcRenderer.send('window-maximize');
});

closeBtn.addEventListener('click', () => {
  ipcRenderer.send('window-close');
});

// 绑定AI分析按钮事件
analyzeBtn.addEventListener('click', () => {
  const textValue = textInput.value.trim();
  if (textValue) {
    statusText.textContent = '正在分析...';
    statusIndicator.className = 'status-indicator';
    
    // 触发AI分析
    ipcRenderer.send('trigger-analysis');
  } else {
    alert('请输入要分析的内容');
  }
});

// 监听文本框变化事件，当有内容输入时也可以触发分析
textInput.addEventListener('input', (event) => {
  // 当文本框有内容时，更新状态提示
  if (event.target.value.trim()) {
    statusText.textContent = '已输入内容，可点击分析';
    statusIndicator.classList.add('status-active');
  } else {
    statusText.textContent = '就绪';
    statusIndicator.classList.remove('status-active');
  }
});

// 监听来自主进程的分析结果
ipcRenderer.on('analysis-result', (event, result) => {
  statusText.textContent = '分析完成';
  statusIndicator.classList.add('status-active');
  
  // 显示分析结果
  resultContent.textContent = typeof result === 'object' 
    ? JSON.stringify(result, null, 2) 
    : result;
  analysisResult.style.display = 'block';
});

// 定期更新监控状态显示
setInterval(() => {
  const now = new Date();
  monitorStatus.textContent = `监控中 | ${now.toLocaleTimeString()}`;
}, 1000);

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
  console.log('CandleMind Desktop App loaded');
  statusText.textContent = '就绪';
});