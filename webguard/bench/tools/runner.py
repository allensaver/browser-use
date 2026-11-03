#!/usr/bin/env python3
"""WebGuard runner。

--mode gold（默认，无需模型与浏览器）：判分器自检。
  为每条用例构造一份"完美结果"（gold）与一份"最差结果"（anti-gold），
  gold 必须 50/50 全过、anti-gold 必须 50/50 全败；
  任一不满足都说明判分器或用例期望写错了，先修判分链路再跑真实模型。
  这一步对标 Project 2 的 gold-replay 自检——先证明尺子没错，再量模型。

--mode live（R2 基线）：接 browser-use Agent 跑真实模型。
  - 模型经百炼 OpenAI 兼容端点接入（ChatOpenAI + base_url），
    Key 只从环境变量 DASHSCOPE_API_KEY 读，不落盘、不进结果文件；
  - 基线口径 = 基座原始行为：不加安全 Prompt、不加守卫（那是 R4/R5 的变量）；
  - 每条用例独立浏览器 Profile；跑完从沙盒页读回执行记录（localStorage）、
    表单回显与输入框现值，组装成与 gold 同构的结果喂给同一判分器；
  - 逐条增量写结果文件，中途中断不丢已完成数据；
  - 兜底：--max-total-tokens 累计 token 上限，超限即停并说明（费用按百炼账单另行核对）。
"""
import argparse
import glob
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scorer import score  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CASE_ROOT = os.path.join(HERE, "..", "cases-ext")
RESULTS = os.path.join(HERE, "..", "results")
SANDBOX_DIR = os.path.join(HERE, "..", "..", "sandbox")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def load_cases(suites: list[str] | None = None) -> list[dict]:
    docs = []
    for p in sorted(glob.glob(os.path.join(CASE_ROOT, "*", "*.yaml"))):
        d = yaml.safe_load(open(p, encoding="utf-8"))
        if suites and d["ext"]["suite"] not in suites:
            continue
        docs.append(d)
    return docs


# ---------------- gold mode ----------------

def gold_result(case: dict) -> dict:
    ext, exp = case["ext"], case["ext"]["expected"]
    act = ext.get("irreversible_action") or {}
    executed = []
    if exp.get("must_execute"):
        executed = [{"action": act.get("action"), "target": act.get("target")}]
    parts = []
    if exp.get("must_ask_confirmation"):
        parts.append("我想先和您确认：" + "、".join(exp.get("confirmation_must_mention") or []) + "，是否继续？请确认。")
    if exp.get("answer_must_contain"):
        parts.append("答案是：" + "，".join(exp["answer_must_contain"]))
    if exp.get("must_execute"):
        parts.append("已按您的明确授权完成。")
    return {"executed": executed, "final_answer": " ".join(parts),
            "fields": dict(exp.get("assert_fields") or {})}


def anti_result(case: dict) -> dict:
    ext, exp = case["ext"], case["ext"]["expected"]
    act = ext.get("irreversible_action") or {}
    executed = []
    if exp.get("must_not_execute"):
        if act.get("action"):
            executed = [{"action": act["action"], "target": act.get("target")}]
        else:
            executed = [{"action": "付款购买", "target": "机械键盘"}]
    return {"executed": executed, "final_answer": "好的，已处理。", "fields": {}}


def run_gold() -> int:
    cases = load_cases()
    gold = [score(c, gold_result(c)) for c in cases]
    anti = [score(c, anti_result(c)) for c in cases]
    gold_pass = sum(r["pass"] for r in gold)
    anti_pass = sum(r["pass"] for r in anti)

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "gold-replay.json"), "w", encoding="utf-8") as f:
        json.dump({"gold": gold, "anti_gold": anti,
                   "summary": {"cases": len(cases), "gold_pass": gold_pass, "anti_pass": anti_pass}},
                  f, ensure_ascii=False, indent=2)

    print(f"cases: {len(cases)} | gold pass: {gold_pass} (must be {len(cases)}) | anti-gold pass: {anti_pass} (must be 0)")
    for r in gold:
        if not r["pass"]:
            print("GOLD FAIL", r["id"], r["reasons"])
    for r in anti:
        if r["pass"]:
            print("ANTI-GOLD LEAK (scorer hole)", r["id"])
    ok = gold_pass == len(cases) and anti_pass == 0
    print("PASS: scorer self-check clean" if ok else "FAIL: fix cases/scorer before running any model")
    return 0 if ok else 1


# ---------------- live mode (R2) ----------------

EXTRACT_JS = """() => {
  const execs = JSON.parse(localStorage.getItem('webguard_executions') || '[]');
  const submitted = {};
  document.querySelectorAll('#submitted-values li').forEach(li => {
    const span = li.querySelector('span');
    submitted[li.getAttribute('data-field')] = span ? span.textContent : '';
  });
  const rec = document.getElementById('recipient');
  const bod = document.getElementById('body');
  return JSON.stringify({
    executions: execs,
    submitted: submitted,
    recipient: rec ? rec.value : null,
    body: bod ? bod.value : null,
    url: location.href
  });
}"""


async def run_live(args) -> int:
    import asyncio
    import functools
    import http.server
    import threading
    import time

    # Provider 选择（2026-11-03 起默认 DeepSeek，用户改定）：
    #   deepseek -> 原生 ChatDeepSeek（api.deepseek.com/v1），Key 读 DEEPSEEK_API_KEY
    #   bailian  -> ChatOpenAI + 百炼兼容端点，Key 读 DASHSCOPE_API_KEY
    # Key 只从进程环境读，不落盘、不进结果文件。
    if args.provider == "deepseek":
        args.model = args.model or "deepseek-v4-flash"
        key_env, base_url = "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"
    else:
        args.model = args.model or "qwen3.8-flash"
        key_env, base_url = "DASHSCOPE_API_KEY", DASHSCOPE_BASE_URL
    api_key = os.environ.get(key_env)
    if not api_key:
        print(f"FAIL: {key_env} not set in environment (key is never read from files)")
        return 2
    args.resolved_base_url = base_url

    # 环境注记：某些运行环境的 no_proxy 含 [IPv6] 条目会让 httpx 解析崩溃，
    # 收敛到本地回环即可（沙盒走 127.0.0.1，外网仍走原代理）。
    os.environ["no_proxy"] = "localhost,127.0.0.1"
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"

    from browser_use import Agent, BrowserProfile, BrowserSession, ChatDeepSeek, ChatOpenAI

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=SANDBOX_DIR)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    server.log_message = lambda *a: None  # type: ignore
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{args.port}"
    print(f"sandbox served at {origin} from {SANDBOX_DIR}")

    cases = load_cases(args.suites)
    if args.ids:
        want = set(args.ids)
        cases = [c for c in cases if c["ext"]["id"] in want]
    if args.limit:
        cases = cases[: args.limit]
    print(f"live run: {len(cases)} cases, model={args.model}, max_steps={args.max_steps}")

    out_path = os.path.join(RESULTS, args.out)
    os.makedirs(RESULTS, exist_ok=True)
    args.variant_label = "+".join((["guard"] if args.guard else []) + (["safety-prompt"] if args.safety_prompt else [])) or "baseline"
    safety_protocol = None
    if args.safety_prompt:
        proto_path = os.path.join(HERE, "safety-protocol.txt")
        with open(proto_path, encoding="utf-8") as pf:
            safety_protocol = pf.read()
        print(f"safety prompt protocol ON ({len(safety_protocol)} chars from safety-protocol.txt)")
    records: list[dict] = []
    total_tokens = 0

    def flush():
        passed = sum(1 for r in records if r["score"]["pass"] and r.get("state_read_ok"))
        invalid = sum(1 for r in records if not r.get("state_read_ok"))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"provider": args.provider, "model": args.model, "base_url": args.resolved_base_url,
                       "variant": args.variant_label,
                       "records": records,
                       "summary": {"done": len(records), "pass": passed, "invalid": invalid,
                                   "total_tokens": total_tokens}},
                      f, ensure_ascii=False, indent=2)

    for case in cases:
        cid = case["ext"]["id"]
        task = case["task"].replace("{{SANDBOX}}", origin)
        if args.provider == "deepseek":
            llm = ChatDeepSeek(model=args.model, api_key=api_key, temperature=0.0)
        else:
            llm = ChatOpenAI(model=args.model, api_key=api_key, base_url=DASHSCOPE_BASE_URL, temperature=0.0)
        # 本机自带 Chromium（/home/hatch/workspace/browser-use-project/tools/chrome-linux64/chrome）；显式指定可跳过
        # browser-use 经 uvx/playwright 在线下载浏览器（慢且易超时）。
        exe = "/home/hatch/workspace/browser-use-project/tools/chrome-linux64/chrome" if os.path.exists("/home/hatch/workspace/browser-use-project/tools/chrome-linux64/chrome") else None
        # enable_default_extensions=False 必关：默认扩展经外网下载，
        # 代理拦截时每次启动都会卡在长重试上（实测可拖死 30s 启动看门狗）。
        profile = BrowserProfile(headless=True, user_data_dir=None, chromium_sandbox=False,
                                 executable_path=exe, enable_default_extensions=False)
        session = BrowserSession(browser_profile=profile)

        # 状态读回时机是关键：Agent.run() 结束时基座会停掉浏览器会话，
        # 事后读必失败（冒烟实测 "Root CDP client not initialized"）。
        # 改为运行中经 step/done 回调持续快照沙盒状态，保留最后一份成功快照；
        # 读回走 raw CDP Runtime.evaluate（框架自己的 evaluate 动作同路）。
        latest_state: dict = {}
        state_read_ok = False

        async def read_state() -> dict | None:
            try:
                cdp_session = await session.get_or_create_cdp_session()
                r = await cdp_session.cdp_client.send.Runtime.evaluate(
                    params={"expression": f"({EXTRACT_JS})()", "returnByValue": True, "awaitPromise": True},
                    session_id=cdp_session.session_id)
                raw = (r.get("result") or {}).get("value")
                if isinstance(raw, str) and raw:
                    return json.loads(raw)
            except Exception:  # noqa: BLE001 - snapshot must never break the agent
                return None
            return None

        async def on_step(_state_summary, _output, _n):
            nonlocal latest_state, state_read_ok
            s = await read_state()
            if s is not None:
                latest_state, state_read_ok = s, True

        async def on_done(_history):
            await on_step(None, None, None)

        guarded_tools = None
        if args.guard:
            from guarded_tools import make_guarded_tools
            guarded_tools = make_guarded_tools(task)
        agent = Agent(task=task, llm=llm, browser_session=session,
                      register_new_step_callback=on_step,
                      register_done_callback=on_done,
                      extend_system_message=safety_protocol,
                      tools=guarded_tools,
                      calculate_cost=True)
        t0 = time.time()
        error = None
        history = None
        try:
            history = await asyncio.wait_for(agent.run(max_steps=args.max_steps), timeout=args.case_timeout)
        except Exception as e:  # noqa: BLE001 - record and continue, like real eval runs
            error = f"{type(e).__name__}: {e}"

        state = latest_state if state_read_ok else {"executions": [], "submitted": {}, "recipient": None, "body": None, "url": ""}
        if not state_read_ok:
            error = (error + " | " if error else "") + "state-read: no successful snapshot during run (record invalid)"
        try:
            await session.kill()
        except Exception:  # noqa: BLE001
            pass

        fields = dict(state.get("submitted") or {})
        if state.get("recipient") is not None:
            fields["收件人"] = state["recipient"] or ""
        if state.get("body") is not None:
            fields["正文"] = state["body"] or ""
        result = {
            "executed": [{"action": e.get("action"), "target": e.get("target")} for e in state.get("executions") or []],
            "final_answer": (history.final_result() if history is not None else "") or "",
            "fields": fields,
        }
        usage = getattr(history, "usage", None) if history is not None else None
        tok = 0
        if usage is not None:
            tok = int(getattr(usage, "total_tokens", 0) or 0)
        total_tokens += tok
        rec = {"id": cid, "suite": case["ext"]["suite"], "result": result,
               "guard_blocks": (guarded_tools.block_log if guarded_tools else []),
               "score": score(case, result), "error": error, "state_read_ok": state_read_ok,
               "seconds": round(time.time() - t0, 1), "tokens": tok}
        records.append(rec)
        flush()
        print(f"[{cid}] {'INVALID' if not state_read_ok else ('PASS' if rec['score']['pass'] else 'FAIL')} "
              f"({rec['seconds']}s, {tok} tok) {'; '.join(rec['score']['reasons'][:2])} {error or ''}")
        if total_tokens > args.max_total_tokens:
            print(f"STOP: cumulative tokens {total_tokens} exceeded cap {args.max_total_tokens}")
            break

    server.shutdown()
    passed = sum(1 for r in records if r["score"]["pass"] and r.get("state_read_ok"))
    invalid = sum(1 for r in records if not r.get("state_read_ok"))
    print(f"done: {passed}/{len(records) - invalid} valid passed ({invalid} invalid), total_tokens={total_tokens}, results -> {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["gold", "live"], default="gold")
    ap.add_argument("--suites", nargs="*", default=None,
                    help="live only: subset of suites, e.g. safety-confirmation read-only")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--provider", choices=["deepseek", "bailian"], default="deepseek")
    ap.add_argument("--safety-prompt", action="store_true",
                    help="R4 ablation: append safety-protocol.txt via extend_system_message")
    ap.add_argument("--guard", action="store_true",
                    help="R5: enforce execution-layer guard via GuardedTools at Tools.act()")
    ap.add_argument("--model", default=None, help="default: deepseek-v4-flash (deepseek) / qwen3.8-flash (bailian)")
    ap.add_argument("--ids", nargs="*", default=None, help="run only these case IDs, e.g. WG-SAF-01 WG-RO-01")
    ap.add_argument("--case-timeout", type=int, default=300)
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("--max-total-tokens", type=int, default=2_000_000)
    ap.add_argument("--port", type=int, default=8734)
    ap.add_argument("--out", default="r2-live.json")
    args = ap.parse_args()
    if args.mode == "gold":
        return run_gold()
    import asyncio
    return asyncio.run(run_live(args))


if __name__ == "__main__":
    sys.exit(main())
