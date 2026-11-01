# WebGuard（R1 阶段）

基于开源 Web Agent 项目 [browser-use](https://github.com/browser-use/browser-use)（MIT，基座版本 v0.13.14 / commit `b49a68e`）的个人改造项目。
基座归原作者；本人的增量是：**可复现的中文任务评测集 + 确定性判分器 + 后续的安全确认策略与执行层守卫**（规划见仓库外文档《基座解剖与改造清单》R1–R6）。

## R1 已完成（2026-11-01）

50 条中文 Web 任务用例，跑在本地确定性沙盒站点上（不用真实电商/邮箱作考场：登录、验证码、改版会让评测不可复现）：

| 套件 | 条数 | 内容 |
|---|---|---|
| safety-confirmation | 12 | 付款/删除/发送/退订/注销/清数据/取消预约的确认规则；10 条必须先确认 + 2 条预授权正对照；含页面 prompt injection 样本 |
| read-only | 15 | 查价、比价算差、计数、库存、账户信息等只读提取 |
| form-multi-step | 15 | 预约表单多字段填写、下拉/单选/日期、同句改口纠正、条款拦截后恢复、只起草不发送 |
| fuzzy-correction | 8 | 指代不清、意图中途撤销、缺货应变、弹窗干扰、条件未达阈值、信息缺失先问 |

安全套件的分级（L0/L1/L2）与确认规则（催促≠确认、换对象重新确认、同句预授权不重复问）
与姊妹项目 Project 2（车载语音 Agent 评测）同口径，便于跨场景对比。

## 判分为什么不用 LLM Judge

上游自带评测只有 2 条任务且靠 LLM Judge 二值判分。本项目改用确定性判分：
沙盒把每次不可逆点击写进页面执行记录，最终回复与表单回显按规则断言——同一条用例跑多少次判分都一致。
Judge 分数保留在用例的 `judge_context` 里作对照，不作主判分。

## 自检（尺子先行）

```bash
python3 bench/tools/validate_cases.py
# cases: 50 | PASS（格式/ID/触发元素真实存在/标准答案有页面依据）

python3 bench/tools/runner.py --mode gold
# gold pass: 50 | anti-gold pass: 0 | PASS（判分器自检：完美结果全过、最差结果全败）
```

两项自检全过，才进入 R2（接真实模型跑基线）。沙盒本地预览：
`python3 -m http.server -d sandbox 8000` 后打开 `http://localhost:8000/shop.html` 等页面。

## 目录

- `sandbox/`：四类确定性模拟站点（商城、邮箱、后台、账户）+ 预约表单页
- `bench/cases-ext/`：四套用例（YAML，上游格式 + `ext` 确定性断言字段）
- `bench/tools/`：`validate_cases.py` 校验器、`scorer.py` 判分器、`runner.py` 运行器（gold 模式已可用，live 模式属 R2）
- `bench/results/gold-replay.json`：最近一次自检的逐条结果
