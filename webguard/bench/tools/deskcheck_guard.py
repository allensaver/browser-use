"""守卫授权规则离线核对（不跑模型）。

对全部 50 条用例做静态验证：
- must_execute 用例：守卫必须放行其金标不可逆动作（否则守卫会误拦正常任务）；
- must_not_execute(any) 用例：该页面全部不可逆动作都必须被守卫拦下；
- must_not_execute(specific)：被禁的指定动作必须被拦下。
全部一致才算通过；任何冲突都说明授权规则需要修订，不允许带冲突接入实跑。
"""
from __future__ import annotations

import glob
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import is_authorized  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CASE_ROOT = os.path.join(HERE, "..", "cases-ext")
SANDBOX = os.path.join(HERE, "..", "..", "sandbox")

LABEL2CODE = {
    "付款购买": "pay", "发送邮件": "send_mail", "删除邮件": "delete_mail",
    "清空收件箱": "delete_all_mail", "清空全部数据": "delete_all_data",
    "删除用户": "delete_user", "注销账号": "close_account",
    "退订会员": "cancel_membership", "取消预约": "cancel_appointment",
    "提交表单": "submit_form",
}
RECIPIENTS = ["李建国", "李志强"]


def page_pairs(page: str) -> list[tuple[str, str, str]]:
    html = open(os.path.join(SANDBOX, page), encoding="utf-8").read()
    pairs = []
    for code, target in re.findall(r'data-action="([^"]+)" data-target="([^"]+)"', html):
        if (code, target) not in [(c, t) for c, t, _ in pairs]:
            pairs.append((code, target, target))
    # 发送/提交的日志名与对象在 sandbox.js 语义里单独处理
    if page == "mail.html":
        pairs.append(("send_mail", "邮件", "邮件"))
    if page == "form.html":
        pairs.append(("submit_form", "服务预约表单", "服务预约表单"))
    return pairs


def main() -> int:
    conflicts = []
    checked = 0
    for p in sorted(glob.glob(os.path.join(CASE_ROOT, "*", "*.yaml"))):
        d = yaml.safe_load(open(p, encoding="utf-8"))
        ext, exp = d["ext"], d["ext"]["expected"]
        task = d["task"].replace("{{SANDBOX}}", "SANDBOX")
        cid, page = ext["id"], ext["sandbox_page"]
        focal = ext.get("irreversible_action") or {"action": "", "target": ""}
        fcode = LABEL2CODE.get(focal["action"], "")
        ftarget = focal["target"]

        def auth(code: str, target: str) -> bool:
            if code == "send_mail":
                # 运行时以收件人栏为准：任务里点名的收件人才是现实对象
                named = [r for r in RECIPIENTS if r in task]
                tgt = named[0] if named else "邮件"
                return is_authorized(task, code, tgt)
            return is_authorized(task, code, target)

        if exp.get("must_execute"):
            checked += 1
            if fcode and not auth(fcode, ftarget):
                conflicts.append(f"{cid}: must_execute 但守卫会拦截 {focal['action']}/{ftarget}")
        if exp.get("must_not_execute"):
            checked += 1
            if exp.get("forbidden_scope") == "any":
                for code, target, _ in page_pairs(page):
                    if auth(code, target):
                        conflicts.append(f"{cid}: must_not_execute(any) 但守卫会放行 {code}/{target}")
            else:  # specific
                if fcode and auth(fcode, ftarget):
                    conflicts.append(f"{cid}: must_not_execute(specific) 但守卫会放行 {focal['action']}/{ftarget}")
    print(f"checked {checked} execution expectations across 50 cases")
    if conflicts:
        print(f"CONFLICTS ({len(conflicts)}):")
        for c in conflicts:
            print(" -", c)
        return 1
    print("PASS: guard authorization agrees with every case expectation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
