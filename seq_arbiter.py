#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seq_arbiter.py — 多操作序列仲裁器（纯 Python 标准库，单文件）

功能
----
输入数据初始状态（名称 -> 值）与多条并发操作序列，按确定性的仲裁规则
交错应用所有操作，输出最终状态、错误清单、冲突记录与完整仲裁历史。

输入格式（JSON 文件或标准输入）
------------------------------
{
  "initial_state": {"a": 1, "b": 2},
  "sequences": [
    {
      "name": "seq1",
      "arrival": 1,                      // 可选，到达顺序号，越小越早；缺省按出现位置
      "operations": [
        {"target": "a", "action": "set", "value": 10},
        {"target": "c", "action": "add", "value": 5},
        {"target": "b", "action": "delete"}
      ]
    }
  ]
}

动作（action）支持三类，兼容中英文别名：
  add    / 增 / 新增      —— 新增数据项（需 value；目标已存在则报错并跳过）
  delete / 删 / 删除 / del —— 删除数据项（目标不存在则报错并跳过）
  set    / 改 / 改值 / modify / update —— 修改值（需 value；目标不存在则报错并跳过）

仲裁规则（自定，理由如下）
------------------------
1. 到达顺序：按序列的 arrival 字段升序，缺省取输入文件中的位置。
   理由：显式、确定、可复现；用户可通过 arrival 显式控制先后。
2. 交错应用：轮转（round-robin）——每一轮中，每条序列按到达顺序各交出
   自己的下一个操作，直到所有序列耗尽。
   理由：公平（无饥饿）、确定性、贴近“多序列并发到达、逐条交错执行”的语义；
   全局因此存在一个唯一的操作总序，便于审计。
3. 同项冲突裁决：最后写入者胜（last-writer-wins），以全局仲裁总序为准。
   即后被执行的操作覆盖先执行的操作，冲突双方均记入 conflicts。
   理由：规则简单、结果确定、无需额外优先级配置，且历史完整可追溯；
   若需“优先级取胜”，只需调整 arrival 即可表达。
4. 一致性校验：操作应用前检查目标存在性（删/改要求存在，增要求不存在），
   失败操作不生效并记入 errors；状态在所有序列间共享、跨操作延续，
   因此“删除后再改”会被正确判为错误。

错误报告类型
------------
  invalid_operation        操作格式非法（缺字段 / 未知动作 / 缺 value）
  duplicate_in_sequence    同序列内重复操作（同动作同目标），跳过不应用
  duplicate_across_sequences 序列间重复操作（同动作同目标），仍应用但报告
  target_not_found         删/改的目标不存在（含“删除后再改”）
  target_exists            增的目标已存在

用法
----
  python3 seq_arbiter.py input.json              # 从文件读取，报告打印到 stdout
  python3 seq_arbiter.py input.json -o report.json
  cat input.json | python3 seq_arbiter.py        # 从标准输入读取
  python3 seq_arbiter.py --example               # 打印一份示例输入
  python3 seq_arbiter.py --selftest              # 运行内置自测

退出码：0 正常（数据级错误体现在报告的 errors 中）；2 输入无法解析。
"""

import argparse
import json
import sys

ACTION_ALIASES = {
    "add": "add", "增": "add", "新增": "add",
    "delete": "delete", "del": "delete", "删": "delete", "删除": "delete",
    "set": "set", "改": "set", "改值": "set", "modify": "set", "update": "set",
}
NEED_VALUE = {"add", "set"}

EXAMPLE_INPUT = {
    "initial_state": {"a": 1, "b": 2},
    "sequences": [
        {
            "name": "seq1",
            "arrival": 1,
            "operations": [
                {"target": "a", "action": "set", "value": 10},
                {"target": "c", "action": "add", "value": 5},
                {"target": "c", "action": "set", "value": 6},
            ],
        },
        {
            "name": "seq2",
            "arrival": 2,
            "operations": [
                {"target": "a", "action": "set", "value": 99},
                {"target": "b", "action": "delete"},
                {"target": "b", "action": "set", "value": 7},
                {"target": "c", "action": "add", "value": 5},
            ],
        },
    ],
}


def _err(errors, etype, seq_name, op_index, op, message):
    errors.append({
        "type": etype,
        "sequence": seq_name,
        "op_index": op_index,
        "operation": op,
        "message": message,
    })


def normalize_sequences(spec, errors):
    """解析并校验序列列表，返回带到达顺序与位置信息的序列字典列表。"""
    raw_seqs = spec.get("sequences", [])
    if not isinstance(raw_seqs, list):
        raise ValueError("'sequences' 必须是列表")
    sequences = []
    for pos, raw in enumerate(raw_seqs):
        if not isinstance(raw, dict):
            raise ValueError("第 %d 条序列不是对象" % (pos + 1))
        name = raw.get("name", "seq#%d" % (pos + 1))
        arrival = raw.get("arrival", pos)
        ops = raw.get("operations", [])
        if not isinstance(ops, list):
            raise ValueError("序列 %r 的 'operations' 必须是列表" % name)
        sequences.append({
            "name": name,
            "arrival": arrival,
            "position": pos,
            "operations": ops,
            "seen": set(),  # 序列内 (action, target) 去重
        })
    return sequences


def arbitrate(spec):
    """核心仲裁：交错应用所有序列的操作，返回完整报告字典。"""
    errors, conflicts, history = [], [], []

    initial = spec.get("initial_state", {})
    if not isinstance(initial, dict):
        raise ValueError("'initial_state' 必须是对象（名称 -> 值）")
    state = dict(initial)

    sequences = normalize_sequences(spec, errors)
    # 到达顺序：arrival 升序，并列时按输入位置，保证确定性
    order = sorted(sequences, key=lambda s: (s["arrival"], s["position"]))

    last_writer = {}   # target -> 最近成功写入的序列名（用于冲突检测）
    seen_global = {}   # (action, target) -> (seq_name, op_index)（跨序列重复检测）
    pointers = {id(s): 0 for s in sequences}
    step = 0

    def apply_op(seq, op_index, raw_op):
        nonlocal step
        step += 1
        seq_name = seq["name"]
        record = {
            "step": step,
            "sequence": seq_name,
            "op_index": op_index,
            "operation": raw_op,
            "result": None,
            "note": "",
        }

        # ---- 1. 格式校验 ----
        if not isinstance(raw_op, dict):
            _err(errors, "invalid_operation", seq_name, op_index, raw_op,
                 "操作必须是对象，含 target/action[/value]")
            record["result"] = "error"
            history.append(record)
            return
        target = raw_op.get("target")
        action = ACTION_ALIASES.get(str(raw_op.get("action", "")).lower())
        value = raw_op.get("value")
        if not target or action is None:
            _err(errors, "invalid_operation", seq_name, op_index, raw_op,
                 "缺少 target 或 action 非法（支持 add/delete/set 及中文别名）")
            record["result"] = "error"
            history.append(record)
            return
        if action in NEED_VALUE and "value" not in raw_op:
            _err(errors, "invalid_operation", seq_name, op_index, raw_op,
                 "动作 %r 需要 value 字段" % action)
            record["result"] = "error"
            history.append(record)
            return

        record.update({"target": target, "action": action, "value": value})
        key = (action, target)

        # ---- 2. 序列内重复操作：报告并跳过 ----
        if key in seq["seen"]:
            _err(errors, "duplicate_in_sequence", seq_name, op_index, raw_op,
                 "同序列内重复操作（%s %s），已跳过" % (action, target))
            record["result"] = "skipped"
            record["note"] = "duplicate_in_sequence"
            history.append(record)
            return
        seq["seen"].add(key)

        # ---- 3. 序列间重复操作：报告但仍应用 ----
        if key in seen_global and seen_global[key][0] != seq_name:
            prev_seq, prev_idx = seen_global[key]
            _err(errors, "duplicate_across_sequences", seq_name, op_index, raw_op,
                 "与序列 %r 的第 %d 个操作重复（%s %s）" % (prev_seq, prev_idx, action, target))
            record["note"] = "duplicate_across_sequences"
        else:
            seen_global.setdefault(key, (seq_name, op_index))

        # ---- 4. 一致性校验并应用 ----
        before = state.get(target, None)
        existed = target in state
        if action == "add" and existed:
            _err(errors, "target_exists", seq_name, op_index, raw_op,
                 "目标 %r 已存在，add 失败" % target)
            record["result"] = "error"
        elif action in ("delete", "set") and not existed:
            _err(errors, "target_not_found", seq_name, op_index, raw_op,
                 "目标 %r 不存在（可能已被删除或从未创建），%s 失败" % (target, action))
            record["result"] = "error"
        else:
            # 同项冲突：不同序列此前成功写过该项，本次覆盖，按 last-writer-wins
            if target in last_writer and last_writer[target] != seq_name:
                conflicts.append({
                    "item": target,
                    "previous_writer": last_writer[target],
                    "current_writer": seq_name,
                    "current_op_index": op_index,
                    "step": step,
                    "rule": "last-writer-wins（按全局仲裁顺序，后执行者覆盖先执行者）",
                })
            if action == "add" or action == "set":
                state[target] = value
            else:  # delete
                del state[target]
            last_writer[target] = seq_name
            record["result"] = "applied"

        record["before"] = before
        record["after"] = state.get(target, None)
        history.append(record)

    # ---- 轮转交错：每轮每条序列（按到达顺序）各执行下一个操作 ----
    while True:
        progressed = False
        for seq in order:
            idx = pointers[id(seq)]
            if idx < len(seq["operations"]):
                pointers[id(seq)] = idx + 1
                apply_op(seq, idx + 1, seq["operations"][idx])  # op_index 从 1 起
                progressed = True
        if not progressed:
            break

    return {
        "final_state": state,
        "errors": errors,
        "conflicts": conflicts,
        "history": history,
        "meta": {
            "arrival_order": [s["name"] for s in order],
            "interleave": "round-robin（每轮每条序列按到达顺序各执行一个操作）",
            "conflict_rule": "last-writer-wins（按全局仲裁顺序）",
            "total_steps": step,
            "error_count": len(errors),
            "conflict_count": len(conflicts),
        },
    }


def load_spec(path):
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    return json.loads(text)


def selftest():
    report = arbitrate(EXAMPLE_INPUT)
    # 交错顺序：seq1#1, seq2#1, seq1#2, seq2#2, seq1#3, seq2#3, seq2#4
    assert report["final_state"] == {"a": 99, "c": 6}, report["final_state"]
    types = [e["type"] for e in report["errors"]]
    assert types.count("target_not_found") == 1          # seq2 改已删除的 b
    assert types.count("target_exists") == 1             # seq2 重复 add c
    assert types.count("duplicate_across_sequences") == 2  # 两序列都 set a / add c
    assert len(report["conflicts"]) == 1                 # 仅 a 被两序列成功写
    assert report["meta"]["total_steps"] == 7
    # 序列内重复
    spec2 = {"initial_state": {"x": 1}, "sequences": [{"name": "s", "operations": [
        {"target": "x", "action": "set", "value": 2},
        {"target": "x", "action": "set", "value": 3}]}]}
    r2 = arbitrate(spec2)
    assert r2["final_state"] == {"x": 2}
    assert r2["errors"][0]["type"] == "duplicate_in_sequence"
    print("selftest OK")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="多操作序列仲裁器：交错应用多条操作序列，输出最终状态与错误报告")
    parser.add_argument("input", nargs="?", default="-",
                        help="输入 JSON 文件路径（缺省或 '-' 表示标准输入）")
    parser.add_argument("-o", "--output", help="报告输出文件（缺省打印到标准输出）")
    parser.add_argument("--compact", action="store_true", help="紧凑 JSON 输出")
    parser.add_argument("--example", action="store_true", help="打印示例输入后退出")
    parser.add_argument("--selftest", action="store_true", help="运行内置自测后退出")
    args = parser.parse_args(argv)

    if args.example:
        json.dump(EXAMPLE_INPUT, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.selftest:
        selftest()
        return 0

    try:
        spec = load_spec(args.input)
        report = arbitrate(spec)
    except (OSError, ValueError) as exc:
        print("输入错误: %s" % exc, file=sys.stderr)
        return 2

    indent = None if args.compact else 2
    text = json.dumps(report, ensure_ascii=False, indent=indent)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
