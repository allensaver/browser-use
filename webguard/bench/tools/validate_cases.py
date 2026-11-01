#!/usr/bin/env python3
"""WebGuard 扩展用例校验器（全 50 条）。

校验四件事：
1. 格式：上游 browser-use evaluate_tasks 必需字段齐全，ext 扩展字段完整、expected 自洽；
2. 唯一与计数：ID 全局唯一、各套件数量与编号与设计一致
   （safety 12 / read-only 15 / form 15 / fuzzy 8，共 50）；
3. 真实：不可逆动作的 trigger_text 与 data-target 必须真实存在于对应沙盒页面；
4. 有据：answer_must_contain 的每个字符串必须能在沙盒页面原文中找到
   （计算类答案须显式登记在 computed_answers 里豁免），防止编造页面上没有的"标准答案"；
   表单 assert_fields 的字段名必须与 form.html 的回显字段一致。
"""
import glob
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CASE_ROOT = os.path.join(HERE, "..", "cases-ext")
SANDBOX = os.path.join(HERE, "..", "..", "sandbox")

EXPECTED_SUITES = {
    "safety-confirmation": ("WG-SAF", 12),
    "read-only": ("WG-RO", 15),
    "form-multi-step": ("WG-FM", 15),
    "fuzzy-correction": ("WG-FZ", 8),
}
FORM_FIELDS = {"姓名", "手机号", "城市", "预约日期", "服务类型", "备注"}
ASK_MARKERS = ("确认", "是否", "要不要", "请问", "哪一", "哪个", "缺", "需要您", "麻烦您")


def main() -> int:
    errors: list[str] = []
    all_ids: list[str] = []
    total = 0
    positive = 0

    page_cache: dict[str, str] = {}

    def page_text(page: str) -> str:
        if page not in page_cache:
            p = os.path.join(SANDBOX, page)
            page_cache[page] = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        return page_cache[page]

    for suite, (prefix, want) in EXPECTED_SUITES.items():
        files = sorted(glob.glob(os.path.join(CASE_ROOT, suite, "*.yaml")))
        if len(files) != want:
            errors.append(f"{suite}: expected {want} cases, found {len(files)}")
        suite_ids = []
        for path in files:
            total += 1
            doc = yaml.safe_load(open(path, encoding="utf-8"))
            tag = f"{suite}/{os.path.basename(path)}"
            for field in ("name", "task", "judge_context", "max_steps"):
                if not doc.get(field):
                    errors.append(f"{tag}: missing upstream field '{field}'")
            if "{{SANDBOX}}" not in (doc.get("task") or ""):
                errors.append(f"{tag}: task missing {{{{SANDBOX}}}} placeholder")
            ext = doc.get("ext") or {}
            for field in ("id", "suite", "level", "sandbox_page", "expected"):
                if not ext.get(field):
                    errors.append(f"{tag}: missing ext field '{field}'")
            cid = ext.get("id", "")
            suite_ids.append(cid)
            all_ids.append(cid)
            if not cid.startswith(prefix):
                errors.append(f"{tag}: id {cid} does not start with {prefix}")

            page = ext.get("sandbox_page", "")
            text = page_text(page)
            if not text:
                errors.append(f"{tag}: sandbox page not found: {page}")
                continue

            exp = ext.get("expected") or {}
            if exp.get("must_execute") and exp.get("must_not_execute"):
                errors.append(f"{tag}: must_execute and must_not_execute both true")
            if exp.get("must_execute"):
                positive += 1
            if exp.get("must_ask_confirmation") and not exp.get("confirmation_must_mention"):
                errors.append(f"{tag}: must_ask_confirmation but no confirmation_must_mention")

            act = ext.get("irreversible_action") or {}
            if act.get("trigger_text"):
                if act["trigger_text"] not in text:
                    errors.append(f"{tag}: trigger_text '{act['trigger_text']}' not in {page}")
                if act.get("target") and f'data-target="{act["target"]}"' not in text:
                    errors.append(f"{tag}: data-target '{act['target']}' not in {page}")

            computed = set(exp.get("computed_answers") or [])
            for s in exp.get("answer_must_contain") or []:
                if s not in text and s not in computed:
                    errors.append(f"{tag}: answer_must_contain '{s}' not grounded in {page} and not in computed_answers")

            if page == "form.html":
                for k in (exp.get("assert_fields") or {}):
                    if k not in FORM_FIELDS:
                        errors.append(f"{tag}: assert_fields key '{k}' not a form field")

        want_ids = {f"{prefix}-{i:02d}" for i in range(1, want + 1)}
        if set(suite_ids) != want_ids:
            errors.append(f"{suite}: ids not sequential: {sorted(suite_ids)}")

    if len(all_ids) != len(set(all_ids)):
        errors.append("duplicate ids across suites")

    print(f"cases: {total} (expect 50) | positive controls / must_execute: {positive}")
    if errors:
        print("FAIL:")
        for e in errors:
            print(" -", e)
        return 1
    print("PASS: all 50 cases valid; triggers exist in sandbox; answers grounded in page text")
    return 0


if __name__ == "__main__":
    sys.exit(main())
