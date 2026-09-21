# -*- coding: utf-8 -*-
"""清理 seat_local_state.json 里 account 未归一化的脏会话。

脏会话特征：account 不以 cs_ 开头（而是昵称 /"主账号" / local_pdd:...），shop_name 为空。
这是早期 CDP / 历史帧落库时账号没归一化留下的，会把同一买家拆成多行。

处理规则：
  1) 按 buyer_id 能找到唯一的 cs_ 正规会话 -> 把脏会话消息按 msg_id 去重合并进去，再删掉脏会话；
  2) 找不到正规会话 -> 直接删除（加 --keep-orphan 则保留）。

用法：
  python tools/clean_seat_state.py                # 预演，不写盘
  python tools/clean_seat_state.py --apply        # 真正清理（先自动备份）
"""
import argparse
import json
import shutil
import sys
import time


def is_dirty(sess):
    acct = str((sess or {}).get("account") or "")
    return not acct.startswith("cs_")


def merge_messages(target, extra):
    """把 extra 的消息按 msg_id 去重合并进 target，并回填 msg_count / last_*。返回新增条数。"""
    msgs = target.setdefault("messages", [])
    seen = {str(m.get("msg_id")) for m in msgs}
    added = 0
    for m in extra or []:
        mid = str(m.get("msg_id"))
        if mid in seen:
            continue
        msgs.append(m)
        seen.add(mid)
        added += 1
    msgs.sort(key=lambda m: float(m.get("ts") or 0))
    target["messages"] = msgs
    target["msg_count"] = len(msgs)
    if msgs:
        last = msgs[-1]
        target["last_content"] = last.get("content") or ""
        target["last_role"] = last.get("role") or ""
        target["last_ts"] = float(last.get("ts") or 0)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=r"C:\Users\1\Desktop\PddBridgeAgent\data\seat_local_state.json")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预演）")
    ap.add_argument("--keep-orphan", action="store_true", help="没有正规会话可合并时保留脏会话")
    args = ap.parse_args()

    with open(args.state, encoding="utf-8-sig") as f:
        st = json.load(f)
    sess = st.get("sessions") or {}

    clean_by_buyer = {}
    for k, v in sess.items():
        if not is_dirty(v):
            clean_by_buyer.setdefault(str(v.get("buyer_id")), []).append(k)

    dirty = [k for k, v in sess.items() if is_dirty(v)]
    merged_sess = merged_msgs = dropped = 0
    details = []
    for k in dirty:
        v = sess[k]
        buyer = str(v.get("buyer_id") or "")
        targets = clean_by_buyer.get(buyer, [])
        if len(targets) == 1:
            n = merge_messages(sess[targets[0]], v.get("messages") or [])
            merged_msgs += n
            merged_sess += 1
            details.append("MERGE %s -> %s (+%d msgs)" % (v.get("account"), sess[targets[0]].get("account"), n))
            del sess[k]
        elif args.keep_orphan:
            dropped += 1
            details.append("KEEP  %s buyer=%s (no clean match)" % (v.get("account"), buyer))
        else:
            del sess[k]
            dropped += 1
            details.append("DROP  %s buyer=%s msgs=%d (no clean match)" % (v.get("account"), buyer, len(v.get("messages") or [])))

    print("dirty=%d merged_sessions=%d merged_messages=%d dropped=%d remaining=%d"
          % (len(dirty), merged_sess, merged_msgs, dropped, len(sess)))
    for d in details:
        print("  " + d)

    if not args.apply:
        print("dry-run：未写盘（加 --apply 执行）")
        return 0

    bak = args.state + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.state, bak)
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    print("已写盘，备份：" + bak)
    return 0


if __name__ == "__main__":
    sys.exit(main())
