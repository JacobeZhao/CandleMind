import React, { useState, useEffect } from "react";
import {
  getSettings, saveSettings, getMyIp, testConnection,
  listAIProviders, listAIConfigs, createAIConfig, updateAIConfig,
  deleteAIConfig, activateAIConfig, testAIConfig, testAIConfigDraft,
} from "../api/client";
import { useApp } from "../context/AppContext";
import {
  Save, Eye, EyeOff, CheckCircle, AlertCircle, Search, Copy,
  Globe, Plus, Pencil, Trash2, X, Zap, Brain, Loader,
} from "lucide-react";
import clsx from "clsx";

const inp = "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-white outline-none focus:border-accent transition-colors";
const lbl = "text-xs text-muted mb-1 block";

export default function Settings() {
  const { setConnected } = useApp();

  const [form, setForm] = useState({
    api_key_test: "", api_secret_test: "", api_key_main: "", api_secret_main: "",
    proxy_url: "",
  });
  const [showKey,    setShowKey]    = useState(false);
  const [saving,     setSaving]     = useState(false);
  const [result,     setResult]     = useState(null);
  const [testKeySet, setTestKeySet] = useState(false);
  const [mainKeySet, setMainKeySet] = useState(false);
  const [myIp,       setMyIp]       = useState(null);
  const [ipLoading,  setIpLoading]  = useState(false);
  // per-card connection test: { test: null|{ok,msg}, main: null|{ok,msg} }
  const [connTest,   setConnTest]   = useState({ test: null, main: null });
  const [connTesting, setConnTesting] = useState({ test: false, main: false });

  // AI config modal
  const [showAiModal,  setShowAiModal]  = useState(false);
  const [aiConfigs,    setAiConfigs]    = useState([]);
  const [providers,    setProviders]    = useState([]);
  const [aiView,       setAiView]       = useState("list");
  const [editingAi,    setEditingAi]    = useState(null);
  const [aiForm,       setAiForm]       = useState({
    name: "", provider: "deepseek", api_key: "", base_url: "", model_name: "",
  });
  const [aiSaving,     setAiSaving]     = useState(false);
  const [aiTesting,    setAiTesting]    = useState(false);
  const [aiDraftTesting, setAiDraftTesting] = useState(false);
  const [aiTestResult, setAiTestResult] = useState(null);

  useEffect(() => {
    getSettings().then(({ data }) => {
      setTestKeySet(data.test_key_set); setMainKeySet(data.main_key_set);
      setForm(f => ({
        ...f, proxy_url: data.proxy_url || "",
      }));
    }).catch(() => {});
    listAIProviders().then(({ data }) => setProviders(data)).catch(() => {});
    listAIConfigs().then(({ data }) => setAiConfigs(data)).catch(() => {});
  }, []);

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));

  const handleSave = async () => {
    setSaving(true); setResult(null);
    try {
      const { data } = await saveSettings(form);   // 空字段=保持不变（后端处理）
      setResult({ ok: true, msg: data.message || "保存成功" });
      const { data: s } = await getSettings();
      setTestKeySet(s.test_key_set); setMainKeySet(s.main_key_set); setConnected(s.connected);
      // 清空密钥输入框（已存盘，不回显）
      setForm(f => ({ ...f, api_key_test: "", api_secret_test: "", api_key_main: "", api_secret_main: "" }));
    } catch (e) {
      setResult({ ok: false, msg: e.response?.data?.detail || "保存失败" });
    } finally { setSaving(false); }
  };

  const handleTestConn = async (isTestnet) => {
    const key = isTestnet ? "test" : "main";
    setConnTesting(s => ({ ...s, [key]: true }));
    setConnTest(s => ({ ...s, [key]: null }));
    try {
      const { data } = await testConnection(isTestnet);
      setConnTest(s => ({ ...s, [key]: { ok: true, msg: data.message } }));
    } catch (e) {
      setConnTest(s => ({ ...s, [key]: { ok: false, msg: e.response?.data?.detail || "连接失败" } }));
    } finally {
      setConnTesting(s => ({ ...s, [key]: false }));
    }
  };

  const detectIp = async () => {
    setIpLoading(true); setMyIp(null);
    try { const { data } = await getMyIp(); setMyIp(data); }
    catch (e) { setMyIp({ error: e.response?.data?.detail || "检测失败" }); }
    finally   { setIpLoading(false); }
  };

  // AI helpers
  const openAiModal = () => {
    listAIConfigs().then(({ data }) => setAiConfigs(data)).catch(() => {});
    // 确保 providers 列表已加载
    if (providers.length === 0) {
      listAIProviders().then(({ data }) => setProviders(data)).catch(() => {});
    }
    setAiView("list"); setShowAiModal(true);
  };
  const openAiForm = (cfg = null) => {
    setEditingAi(cfg);
    const def = providers.find(p => p.id === (cfg?.provider || "deepseek")) || {};
    setAiForm(cfg
      ? { name: cfg.name, provider: cfg.provider, api_key: "",
          base_url: cfg.base_url || def.base_url || "",
          model_name: cfg.model_name || def.model || "" }
      : { name: "", provider: "deepseek", api_key: "",
          base_url: def.base_url || "", model_name: def.model || "" });
    setAiTestResult(null); setAiView("form");
  };
  const onProviderChange = (pid) => {
    const def = providers.find(p => p.id === pid) || {};
    setAiForm(f => ({ ...f, provider: pid, base_url: def.base_url || "", model_name: def.model || "" }));
    setAiTestResult(null);
  };

  const aiErrorMessage = (error, fallback) => {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    return detail?.message || error.response?.data?.message || fallback;
  };

  const persistAiForm = async () => {
    if (editingAi) await updateAIConfig(editingAi.id, aiForm);
    else           await createAIConfig(aiForm);
    const { data } = await listAIConfigs();
    setAiConfigs(data);
    setAiView("list");
  };

  const saveAiForm = async () => {
    if (!aiForm.name.trim() || aiSaving || aiTesting || aiDraftTesting) return;
    setAiSaving(true); setAiTestResult(null);
    try {
      await persistAiForm();
    } catch (e) {
      setAiTestResult({ ok: false, msg: aiErrorMessage(e, "保存失败") });
    }
    finally { setAiSaving(false); }
  };

  const saveAndTestAiForm = async () => {
    if (!aiForm.name.trim() || aiSaving || aiTesting || aiDraftTesting) return;
    setAiDraftTesting(true); setAiTestResult(null);
    try {
      const draft = editingAi ? { ...aiForm, config_id: editingAi.id } : aiForm;
      await testAIConfigDraft(draft);
    } catch (e) {
      setAiTestResult({ ok: false, msg: aiErrorMessage(e, "连接测试失败，配置未保存") });
      setAiDraftTesting(false);
      return;
    }
    try {
      await persistAiForm();
    } catch (e) {
      setAiTestResult({ ok: false, msg: aiErrorMessage(e, "连接成功，但保存失败") });
    } finally {
      setAiDraftTesting(false);
    }
  };

  const testAi = async (id) => {
    if (aiSaving || aiTesting || aiDraftTesting) return;
    setAiTesting(true); setAiTestResult(null);
    try {
      const { data } = await testAIConfig(id);
      setAiTestResult({ ok: true, msg: `连接成功：${data.response}` });
    } catch (e) {
      setAiTestResult({ ok: false, msg: aiErrorMessage(e, "连接失败") });
    } finally { setAiTesting(false); }
  };
  const handleActivateAi = async (id) => {
    await activateAIConfig(id);
    const { data } = await listAIConfigs(); setAiConfigs(data);
  };
  const handleDeleteAi = async (id) => {
    if (!window.confirm("确认删除？")) return;
    await deleteAIConfig(id);
    const { data } = await listAIConfigs(); setAiConfigs(data);
  };

  const activeAi = aiConfigs.find(c => c.is_active);

  return (
    <>
    <div className="space-y-3">

      {/* ── API 配置（测试网 + 真实网 两套，永久保存）── */}
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          <h2 className="text-sm font-semibold">Binance API 配置</h2>
          <span className="text-xs text-muted">两套 API 同时保存（与 AI 模型配置一样存于数据库，改代码不丢）</span>
        </div>
        <div className="grid md:grid-cols-2 gap-4">
          {[["测试网 API", "api_key_test", "api_secret_test", testKeySet, true],
            ["真实网 API", "api_key_main", "api_secret_main", mainKeySet, false]].map(([title, kf, sf, isSet, isTestnet]) => {
            const ck  = isTestnet ? "test" : "main";
            const res = connTest[ck];
            const testing = connTesting[ck];
            return (
            <div key={kf} className="border border-border/60 rounded-xl p-3 bg-surface/30">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs font-semibold text-white">{title}</span>
                {isSet && <span className="text-[10px] text-green flex items-center gap-0.5"><CheckCircle size={11}/>已配置</span>}
              </div>
              <label className={lbl}>API Key</label>
              <div className="relative mb-2">
                <input type={showKey ? "text" : "password"} value={form[kf]}
                  onChange={e => set(kf, e.target.value)}
                  onPaste={e => { e.preventDefault(); set(kf, e.clipboardData.getData("text").trim()); }}
                  placeholder={isSet ? "留空保持不变" : "输入 API Key"} className={inp + " pr-8"} />
                <button onClick={() => setShowKey(!showKey)} className="absolute right-2.5 top-2 text-muted hover:text-white">
                  {showKey ? <EyeOff size={13}/> : <Eye size={13}/>}
                </button>
              </div>
              <label className={lbl}>API Secret</label>
              <input type="password" value={form[sf]}
                onChange={e => set(sf, e.target.value)}
                onPaste={e => { e.preventDefault(); set(sf, e.clipboardData.getData("text").trim()); }}
                placeholder={isSet ? "留空保持不变" : "输入 API Secret"} className={inp} />
              <div className="flex items-center gap-2 mt-2">
                <button onClick={() => handleTestConn(isTestnet)} disabled={testing || !isSet}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-border bg-surface text-muted hover:text-white disabled:opacity-50 transition-colors">
                  {testing ? <Loader size={11} className="animate-spin"/> : <Zap size={11}/>}
                  {testing ? "测试中..." : "测试连接"}
                </button>
                {res && (
                  <span className={clsx("flex items-center gap-1 text-[11px]", res.ok ? "text-green" : "text-red")}>
                    {res.ok ? <CheckCircle size={11}/> : <AlertCircle size={11}/>}
                    {res.msg}
                  </span>
                )}
              </div>
            </div>
            );
          })}
        </div>
        <div className="mt-4">
          <div>
            <label className={lbl}>代理地址（大陆必填）</label>
            <div className="relative">
              <Globe size={12} className="absolute left-2.5 top-2.5 text-muted" />
              <input value={form.proxy_url} onChange={e => set("proxy_url", e.target.value)}
                placeholder="http://127.0.0.1:7897" className={inp + " pl-7"} />
            </div>
          </div>
          <p className="text-[11px] text-muted mt-2">
            测试网 / 真实网切换请使用顶栏右侧的网络 Tab，两套 API 同时在线。
          </p>
        </div>
      </div>

      {/* ── AI 模型配置 ── */}
      <div className="bg-card border border-border rounded-xl p-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold flex items-center gap-2">
              <Brain size={14} className="text-accent"/> AI 模型配置
            </h2>
            <p className="text-xs text-muted mt-0.5">
              {activeAi
                ? `已激活：${activeAi.name}（${activeAi.provider}）`
                : "未配置 AI 模型，AI 辅助功能将不可用"}
            </p>
          </div>
          <button onClick={openAiModal}
            className="flex items-center gap-2 bg-surface border border-border text-sm text-muted hover:text-white px-4 py-2 rounded-lg transition-colors">
            <Brain size={14}/> 管理模型
          </button>
        </div>
      </div>

      {/* ── 操作 ── */}
      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={handleSave} disabled={saving}
          className="flex items-center gap-2 bg-accent text-black px-5 py-2 rounded-lg text-sm font-bold hover:bg-accent/90 transition-colors disabled:opacity-60">
          <Save size={14}/> {saving ? "保存中..." : "保存配置"}
        </button>
        <button onClick={detectIp} disabled={ipLoading}
          className="flex items-center gap-1.5 bg-surface border border-border text-sm text-muted hover:text-white px-4 py-2 rounded-lg transition-colors disabled:opacity-60">
          <Search size={13} className={ipLoading ? "animate-spin" : ""}/>
          {ipLoading ? "检测中..." : "检测出口 IP"}
        </button>
        {result && (
          <div className={clsx("flex items-center gap-1.5 text-sm", result.ok ? "text-green" : "text-red")}>
            {result.ok ? <CheckCircle size={14}/> : <AlertCircle size={14}/>} {result.msg}
          </div>
        )}
        {myIp && !myIp.error && (
          <div className="flex items-center gap-2 text-xs text-green bg-green/5 border border-green/20 rounded-lg px-3 py-2">
            <span>{myIp.via_proxy ? "✓ 代理" : "⚠ 直连"}</span>
            <code className="font-bold">{myIp.ip}</code>
            <button onClick={() => navigator.clipboard?.writeText(myIp.ip)}
              className="flex items-center gap-0.5 text-muted hover:text-white">
              <Copy size={10}/>复制
            </button>
          </div>
        )}
        {myIp?.error && <div className="flex items-center gap-1.5 text-xs text-red"><AlertCircle size={12}/>{myIp.error}</div>}
      </div>
    </div>

    {/* ════════ AI 配置弹窗 ════════ */}
    {showAiModal && (
      <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
        <div className="bg-card border border-border rounded-2xl w-full max-w-lg flex flex-col max-h-[85vh]">
          <div className="flex items-center justify-between px-5 py-4 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              {aiView === "form" && (
                <button onClick={() => setAiView("list")} className="text-muted hover:text-white mr-1">←</button>
              )}
              <Brain size={15} className="text-accent"/>
              <span className="font-semibold text-sm">
                {aiView === "list" ? "AI 模型管理" : editingAi ? "编辑模型" : "新增模型"}
              </span>
            </div>
            <button onClick={() => setShowAiModal(false)} className="text-muted hover:text-white"><X size={16}/></button>
          </div>

          {aiView === "list" && (
            <div className="flex flex-col flex-1 overflow-hidden">
              <div className="px-5 py-3 border-b border-border shrink-0">
                <button onClick={() => openAiForm(null)}
                  className="flex items-center gap-2 bg-accent text-black text-xs font-bold px-4 py-2 rounded-lg hover:bg-accent/90 transition-colors">
                  <Plus size={13}/> 新增模型
                </button>
              </div>
              <div className="flex-1 overflow-y-auto px-5 py-3 space-y-2">
                {aiConfigs.length === 0 ? (
                  <p className="text-muted text-sm text-center py-8">还没有 AI 模型，点击「新增」添加</p>
                ) : aiConfigs.map(c => (
                  <div key={c.id} className={clsx(
                    "flex items-center gap-3 p-3 rounded-xl border transition-colors",
                    c.is_active ? "border-accent/40 bg-accent/5" : "border-border bg-surface/50"
                  )}>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-white truncate">{c.name}</span>
                        {c.is_active && <span className="text-xs bg-accent/20 text-accent px-1.5 py-0.5 rounded font-medium shrink-0">激活</span>}
                      </div>
                      <div className="text-xs text-muted mt-0.5">{c.provider} · {c.model_name}</div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      {!c.is_active && (
                        <button onClick={() => handleActivateAi(c.id)}
                          className="flex items-center gap-1 text-xs px-2 py-1 rounded bg-accent/10 border border-accent/30 text-accent hover:bg-accent/20 transition-colors">
                          <Zap size={11}/> 激活
                        </button>
                      )}
                      <button onClick={() => { openAiForm(c); setAiTestResult(null); }}
                        className="p-1.5 rounded text-muted hover:text-white hover:bg-surface transition-colors">
                        <Pencil size={13}/>
                      </button>
                      <button onClick={() => handleDeleteAi(c.id)}
                        className="p-1.5 rounded text-muted hover:text-red hover:bg-red/10 transition-colors">
                        <Trash2 size={13}/>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {aiView === "form" && (
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
              <div>
                <label className={lbl}>配置名称</label>
                <input value={aiForm.name} onChange={e => setAiForm(f=>({...f,name:e.target.value}))}
                  placeholder="例：我的DeepSeek" className={inp} autoFocus />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className={lbl}>Provider</label>
                  <select value={aiForm.provider} onChange={e => onProviderChange(e.target.value)}
                    className={inp + " cursor-pointer"}>
                    {(providers.length > 0 ? providers : [
                      { id: "openai",     name: "OpenAI" },
                      { id: "deepseek",   name: "DeepSeek" },
                      { id: "qwen",       name: "通义千问 (Qwen)" },
                      { id: "kimi",       name: "Kimi (Moonshot)" },
                      { id: "glm",        name: "智谱 GLM" },
                      { id: "minimax",    name: "MiniMax" },
                      { id: "gemini",     name: "Google Gemini" },
                      { id: "openrouter", name: "OpenRouter" },
                      { id: "litellm",    name: "LiteLLM (本地代理)" },
                      { id: "ollama",     name: "Ollama (本地)" },
                      { id: "claude",     name: "Anthropic Claude" },
                    ]).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
                <div>
                  <label className={lbl}>模型名称</label>
                  <input value={aiForm.model_name} onChange={e => setAiForm(f=>({...f,model_name:e.target.value}))}
                    placeholder="例：deepseek-v4-pro" className={inp} />
                </div>
              </div>
              <div>
                <label className={lbl}>API Key</label>
                <input type="password" value={aiForm.api_key}
                  onChange={e => setAiForm(f=>({...f,api_key:e.target.value}))}
                  placeholder={editingAi?.api_key_set ? "留空保持不变" : "输入 API Key"}
                  className={inp} />
              </div>
              <div>
                <label className={lbl}>API Base URL</label>
                <input value={aiForm.base_url} onChange={e => setAiForm(f=>({...f,base_url:e.target.value}))}
                  placeholder="留空使用默认" className={inp} />
              </div>
              {aiTestResult && (
                <div className={clsx("flex items-center gap-1.5 text-xs p-2 rounded-lg",
                  aiTestResult.ok ? "text-green bg-green/10 border border-green/20" : "text-red bg-red/10 border border-red/20")}>
                  {aiTestResult.ok ? <CheckCircle size={12}/> : <AlertCircle size={12}/>}
                  {aiTestResult.msg}
                </div>
              )}
              <div className="flex flex-wrap gap-3 pt-2">
                <button onClick={saveAiForm} disabled={aiSaving || aiTesting || aiDraftTesting || !aiForm.name.trim()}
                  className="flex items-center gap-2 bg-accent text-black px-5 py-2 rounded-lg text-sm font-bold hover:bg-accent/90 disabled:opacity-60 transition-colors">
                  {aiSaving ? <Loader size={13} className="animate-spin"/> : <Save size={13}/>}
                  {aiSaving ? "保存中..." : "保存"}
                </button>
                <button onClick={saveAndTestAiForm} disabled={aiSaving || aiTesting || aiDraftTesting || !aiForm.name.trim()}
                  className="flex items-center gap-2 bg-surface border border-accent/40 px-4 py-2 rounded-lg text-sm text-accent hover:bg-accent/10 disabled:opacity-60 transition-colors">
                  {aiDraftTesting ? <Loader size={13} className="animate-spin"/> : <Zap size={13}/>}
                  {aiDraftTesting ? "测试并保存中..." : "保存并测试"}
                </button>
                {editingAi && (
                  <button onClick={() => testAi(editingAi.id)} disabled={aiSaving || aiTesting || aiDraftTesting}
                    className="flex items-center gap-2 bg-surface border border-border px-4 py-2 rounded-lg text-sm text-muted hover:text-white disabled:opacity-60 transition-colors">
                    {aiTesting ? <Loader size={13} className="animate-spin"/> : <Zap size={13}/>}
                    {aiTesting ? "测试中..." : "测试已保存配置"}
                  </button>
                )}
                <button onClick={() => setAiView("list")} disabled={aiSaving || aiTesting || aiDraftTesting}
                  className="px-4 py-2 rounded-lg text-sm text-muted border border-border hover:text-white transition-colors">
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    )}
    </>
  );
}
