#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arbiter.py — 多操作序列并发修改同一批数据的仲裁工具（纯标准库，单文件）

设计决策（规则自定，理由如下）
=============================

1. 到达顺序
   输入文件中 sequences 数组的先后顺序即“到达顺序”（S1 先于 S2 ……）。
   理由：确定、可复现，且与调用方的自然表达一致，无需额外时间戳。

2. 交错方式：按到达顺序轮询（round-robin）
   第 r 轮依次从每条未耗尽的序列中取第 r 个操作，按到达顺序逐个仲裁。
   理由：公平（任何序列不会被长期饿死）、确定（同一输入必得同一输出）、
   且每条序列内部的操作顺序被完整保留，序列状态跨操作自然延续。

3. 冲突裁决：同轮同项冲突，先到者胜（FCFS），败者拒绝并记录
   同一轮中若多条序列的操作指向同一数据项，则到达顺序最早的序列的操作
   生效，其余被拒绝并写入仲裁历史与错误清单。
   理由：FCFS 简单、确定、无饥饿；拒绝败者而非叠加执行，避免
   “删了又改”这类组合产生未定义结果。

4. 重复操作
   - 序列内重复（同序列同目标同动作）：报告并跳过后续重复操作。
   - 序列间重复（不同序列同目标同动作）：报告并跳过较晚出现的操作。
   理由：重复操作通常源于上游生成错误；跳过而非应用可避免
   “重复删除导致误报不存在”等次生噪声，同时保留报告便于排查。

5. 一致性校验（应用时执行）
   - add：目标已存在 -> 报错，不应用
   - delete / set：目标不存在（含已被删除）-> 报错，不应用
   删除后再改、删除后再删、重复新增都会被捕获。

输入格式（JSON 文件，或 '-' 表示标准输入）
------------------------------------------
{
  "initial_state": [ {"name": "a", "value": 1}, ... ],   // 也接受 {"a": 1, ...}
  "sequences": [
    {"name": "S1", "ops": [
        {"target": "a", "action": "set", "value": 10},   // 改值
        {"target": "b", "action": "add", "value": 5},    // 新增
        {"target": "c", "action": "delete"}              // 删除
    ]}
  ]
}
action 支持 add/delete/set 及中文别名 增/删/改（改值、改、新增、删除亦可）。

输出（JSON 到标准输出）
----------------------
{
  "final_state": {...},          // 仲裁后的最终状态
  "errors": [...],               // 错误清单（含序列名、操作下标、原因）
  "history": [...],              // 仲裁历史：每操作的轮次、裁决、前后值
  "meta": {...}                  // 仲裁规则说明
}

用法
----
  python3 arbiter.py input.json            # 仲裁并输出 JSON 报告
  python3 arbiter.py input.json --compact  # 紧凑 JSON
  python3 arbiter.py --demo                # 运行内置示例
  cat input.json | python3 arbiter.py -    # 从标准输入读取
退出码：0 = 无错误；1 = 存在错误报告；2 = 输入非法。
"""

import argparse
import json
import sys

# 动作别名归一化
ACTION_ALIASES = {
    "add": "add", "增": "add", "新增": "add",
    "delete": "delete", "删": "delete", "删除": "delete",
    "set": "set", "改": "set", "改值": "set", "修改": "set",
}

RULES_META = {
    "arrival_order": "输入文件中 sequences 的先后顺序",
    "interleave": "按到达顺序轮询（round-robin），每轮每条序列至多一个操作",
    "conflict_rule": "同轮同项冲突先到者胜（FCFS），败者拒绝并记录",
    "duplicate_rule": "序列内/序列间重复操作（同目标同动作）：报告并跳过",
}


def normalize_action(raw):
    """把动作别名归一化为 add/delete/set；非法返回 None。"""
    if not isinstance(raw, str):
        return None
    return ACTION_ALIASES.get(raw.strip().lower()) or ACTION_ALIASES.get(raw.strip())


def parse_initial_state(data):
    """接受 [{name, value}...] 或 {name: value} 两种形式，返回 dict。"""
    if isinstance(data, dict):
        return dict(data)
    state = {}
    if isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict) or "name" not in item:
                raise ValueError("initial_state 第 %d 项缺少 name" % i)
            state[item["name"]] = item.get("value")
    else:
        raise ValueError("initial_state 必须是数组或对象")
    return state


def parse_sequences(data):
    """校验并返回 [(seq_name, [op...]), ...]，保持到达顺序。"""
    if not isinstance(data, list) or not data:
        raise ValueError("sequences 必须是非空数组")
    seqs = []
    for i, seq in enumerate(data):
        if not isinstance(seq, dict):
            raise ValueError("sequences[%d] 必须是对象" % i)
        name = seq.get("name", "seq_%d" % i)
        ops = seq.get("ops", [])
        if not isinstance(ops, list):
            raise ValueError("序列 %s 的 ops 必须是数组" % name)
        seqs.append((str(name), ops))
    return seqs


def arbitrate(initial_state, sequences):
    """核心仲裁。返回 (final_state, errors, history)。"""
    state = dict(initial_state)
    errors = []    # 错误清单
    history = []   # 仲裁历史

    def report(err_type, seq_name, op_idx, op, message):
        errors.append({
            "type": err_type,
            "sequence": seq_name,
            "op_index": op_idx,
            "op": op,
            "message": message,
        })

    def log(round_no, seq_name, op_idx, op, decision, reason,
            target=None, before=None, after=None):
        history.append({
            "round": round_no,
            "sequence": seq_name,
            "op_index": op_idx,
            "op": op,
            "decision": decision,   # applied / rejected / skipped
            "reason": reason,
            "target": target,
            "value_before": before,
            "value_after": after,
        })

    # 预处理：归一化动作，非法动作直接报告并视为不存在
    # prepared[i] = [ (op_idx, normalized_op_or_None), ... ]
    prepared = []
    for seq_name, ops in sequences:
        plist = []
        for idx, op in enumerate(ops):
            if not isinstance(op, dict) or "target" not in op:
                report("invalid_op", seq_name, idx, op, "操作缺少 target 字段")
                plist.append((idx, None))
                continue
            action = normalize_action(op.get("action"))
            if action is None:
                report("invalid_action", seq_name, idx, op,
                       "未知动作: %r（支持 add/delete/set 或 增/删/改）"
                       % (op.get("action"),))
                plist.append((idx, None))
                continue
            if action in ("add", "set") and "value" not in op:
                report("invalid_op", seq_name, idx, op,
                       "%s 操作缺少 value 字段" % action)
                plist.append((idx, None))
                continue
            plist.append((idx, {"target": op["target"], "action": action,
                                "value": op.get("value"), "raw": op}))
        prepared.append(plist)

    # 序列内重复检测：同 (target, action) 的后续出现 -> 报告并跳过
    seen_in_seq = []          # 每条序列一个 set
    skip = [set() for _ in sequences]   # skip[i] = 需跳过的 op_idx 集合
    for i, (seq_name, _) in enumerate(sequences):
        seen = set()
        for idx, nop in prepared[i]:
            if nop is None:
                continue
            key = (nop["target"], nop["action"])
            if key in seen:
                report("duplicate_in_sequence", seq_name, idx, nop["raw"],
                       "序列内重复操作（目标 %r，动作 %s），已跳过"
                       % (nop["target"], nop["action"]))
                skip[i].add(idx)
            else:
                seen.add(key)
        seen_in_seq.append(seen)

    # 序列间重复检测：同 (target, action) 在不同序列出现 -> 较晚者报告并跳过
    seen_global = {}  # key -> (seq_name, op_idx)
    for i, (seq_name, _) in enumerate(sequences):
        for idx, nop in prepared[i]:
            if nop is None or idx in skip[i]:
                continue
            key = (nop["target"], nop["action"])
            if key in seen_global and seen_global[key][0] != seq_name:
                first_seq, first_idx = seen_global[key]
                report("duplicate_across_sequences", seq_name, idx, nop["raw"],
                       "与序列 %s 的操作 #%d 重复（目标 %r，动作 %s），已跳过"
                       % (first_seq, first_idx, nop["target"], nop["action"]))
                skip[i].add(idx)
            else:
                seen_global.setdefault(key, (seq_name, idx))

    # 轮询交错仲裁
    max_len = max(len(p) for p in prepared)
    for round_no in range(max_len):
        # 收集本轮各序列的操作（按到达顺序）
        round_ops = []  # (seq_i, op_idx, nop)
        for i, (seq_name, _) in enumerate(sequences):
            if round_no < len(prepared[i]):
                op_idx, nop = prepared[i][round_no]
                if nop is None:
                    log(round_no, seq_name, op_idx, None, "rejected",
                        "非法操作，未参与仲裁")
                    continue
                if op_idx in skip[i]:
                    log(round_no, seq_name, op_idx, nop["raw"], "skipped",
                        "重复操作，已跳过", target=nop["target"])
                    continue
                round_ops.append((i, op_idx, nop))

        # 同轮同项冲突裁决：FCFS，先到者胜
        winners = {}   # target -> (seq_i, op_idx, nop)
        losers = []    # (seq_i, op_idx, nop, winner_seq_name)
        for i, op_idx, nop in round_ops:
            t = nop["target"]
            if t in winners:
                losers.append((i, op_idx, nop, sequences[winners[t][0]][0]))
            else:
                winners[t] = (i, op_idx, nop)

        for i, op_idx, nop, winner_seq in losers:
            report("conflict_rejected", sequences[i][0], op_idx, nop["raw"],
                   "与序列 %s 同轮冲突（目标 %r），按先到者胜规则被拒绝"
                   % (winner_seq, nop["target"]))
            log(round_no, sequences[i][0], op_idx, nop["raw"], "rejected",
                "同轮冲突败于序列 %s（FCFS）" % winner_seq,
                target=nop["target"],
                before=state.get(nop["target"]),
                after=state.get(nop["target"]))

        # 应用胜者操作（按到达顺序），做一致性校验
        for i, op_idx, nop in sorted(winners.values(), key=lambda w: w[0]):
            seq_name = sequences[i][0]
            t, action, value = nop["target"], nop["action"], nop["value"]
            before = state.get(t)
            if action == "add":
                if t in state:
                    report("add_existing", seq_name, op_idx, nop["raw"],
                           "新增失败：目标 %r 已存在" % t)
                    log(round_no, seq_name, op_idx, nop["raw"], "rejected",
                        "目标已存在", target=t, before=before, after=before)
                    continue
                state[t] = value
            elif action == "delete":
                if t not in state:
                    report("target_not_found", seq_name, op_idx, nop["raw"],
                           "删除失败：目标 %r 不存在（可能已被删除）" % t)
                    log(round_no, seq_name, op_idx, nop["raw"], "rejected",
                        "目标不存在", target=t, before=None, after=None)
                    continue
                del state[t]
            else:  # set
                if t not in state:
                    report("target_not_found", seq_name, op_idx, nop["raw"],
                           "改值失败：目标 %r 不存在（可能已被删除）" % t)
                    log(round_no, seq_name, op_idx, nop["raw"], "rejected",
                        "目标不存在", target=t, before=None, after=None)
                    continue
                state[t] = value
            log(round_no, seq_name, op_idx, nop["raw"], "applied", "ok",
                target=t, before=before, after=state.get(t))

    return state, errors, history


DEMO_INPUT = {
    "initial_state": [
        {"name": "a", "value": 1},
        {"name": "b", "value": 2},
        {"name": "c", "value": 3},
    ],
    "sequences": [
        {"name": "S1", "ops": [
            {"target": "a", "action": "set", "value": 10},
            {"target": "b", "action": "delete"},
            {"target": "b", "action": "set", "value": 20},
            {"target": "d", "action": "add", "value": 4},
        ]},
        {"name": "S2", "ops": [
            {"target": "a", "action": "set", "value": 99},
            {"target": "c", "action": "set", "value": 30},
            {"target": "c", "action": "set", "value": 31},
            {"target": "x", "action": "delete"},
        ]},
        {"name": "S3", "ops": [
            {"target": "c", "action": "set", "value": 300},
            {"target": "d", "action": "add", "value": 4},
        ]},
    ],
}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="多操作序列并发修改仲裁工具（纯标准库单文件）")
    ap.add_argument("input", nargs="?",
                    help="输入 JSON 文件路径，'-' 表示标准输入")
    ap.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        doc = DEMO_INPUT
    else:
        if not args.input:
            ap.error("缺少输入文件（或用 --demo 运行示例）")
        try:
            text = (sys.stdin.read() if args.input == "-"
                    else open(args.input, encoding="utf-8").read())
            doc = json.loads(text)
        except (OSError, json.JSONDecodeError) as e:
            print("输入读取/解析失败: %s" % e, file=sys.stderr)
            return 2

    try:
        initial = parse_initial_state(doc.get("initial_state", []))
        sequences = parse_sequences(doc.get("sequences"))
    except ValueError as e:
        print("输入格式错误: %s" % e, file=sys.stderr)
        return 2

    final_state, errors, history = arbitrate(initial, sequences)
    report = {
        "final_state": final_state,
        "errors": errors,
        "history": history,
        "meta": RULES_META,
    }
    if args.compact:
        json.dump(report, sys.stdout, ensure_ascii=False)
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
