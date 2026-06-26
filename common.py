#!/usr/bin/env python3
"""
飞书跨组织知识库迁移 — 公共基础设施

所有迁移脚本共用的常量、异常类、工具函数、lark-cli 封装、状态管理。
"""

import subprocess
import json
import time
import os
import re
from datetime import datetime

# ============================================================
# 常量
# ============================================================

# 工作目录：存放临时下载/上传的文件，位于脚本同级目录下的 .workdir/
WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".workdir")

RATE_LIMIT_BASE = 2           # 限流退避基数（秒），实际等待 = 2^attempt × base
NETWORK_RETRY_BASE = 5        # 网络异常退避基数（秒）
MAX_RETRIES = 3               # 限流最大重试次数
MAX_NETWORK_RETRIES = 5       # 网络异常最大重试次数
PROGRESS_INTERVAL = 50        # 每迁移 N 个节点打印一次进度并做完整性抽检

# ============================================================
# 异常类
#
# 分层设计：MigrationError 是基类，子类用于触发不同的重试/跳过逻辑
# ============================================================

class MigrationError(Exception):
    """迁移通用错误"""
    pass

class RateLimitError(MigrationError):
    """飞书 API 限流 (错误码 800004135)"""
    pass

class NetworkError(MigrationError):
    """网络不可达/超时等瞬时错误"""
    pass

class LarkPermissionError(MigrationError):
    """权限不足 (错误码 99991679 / 131006)，通常是文档权限未开放"""
    pass

class DocDeletedError(MigrationError):
    """文档已被删除 (错误码 900009)，应跳过而非标记失败"""
    pass

class APIError(MigrationError):
    """API 调用返回非零 code 但不属于上述特定类别"""
    pass

# ============================================================
# 日志
# ============================================================

def log(msg):
    """带时间戳的日志输出，flush=True 确保管道/重定向时实时可见"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ============================================================
# 工具函数
# ============================================================

def parse_lark_json(text):
    """从 lark-cli 的 stdout 中提取 JSON。

    lark-cli 有时会在 JSON 前输出进度信息（如 '[page 1] fetching...'），
    所以需要跳过非 JSON 前缀，找到第一个 { 或 [ 开始解析。
    """
    if not text:
        return None
    text = text.strip()
    for i, ch in enumerate(text):
        if ch in ('{', '['):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue
    return None


def escape_title(text, max_len=200):
    """清理文档标题：去除控制字符、截断过长标题、空标题用默认值"""
    if not text:
        return "未命名文档"
    text = re.sub(r'[\x00-\x1f\x7f]', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text or "未命名文档"

# ============================================================
# 错误检测辅助函数
#
# 通过检查错误信息中的特征字符串来分类异常类型，
# 因为 lark-cli 把 API 错误码混在 stderr/stdout 里返回。
# ============================================================

def _is_rate_limit(msg):
    """检测是否为飞书限流错误"""
    return '800004135' in str(msg)


def _is_network_error(msg):
    """检测是否为网络层瞬时错误（DNS/超时/连接拒绝等）"""
    m = str(msg).lower()
    return any(k in m for k in [
        'no such host', 'timed out', 'network is unreachable',
        'connection refused', 'connection reset', 'eof',
        'i/o timeout', 'broken pipe',
    ])


def _is_permission_error(msg):
    """检测是否为权限错误（无文档访问权限 / 跨组织操作被拒）"""
    return '99991679' in str(msg) or '131006' in str(msg)


def _is_doc_deleted(msg):
    """检测是否为文档已删除错误"""
    return '900009' in str(msg)

# ============================================================
# lark-cli 封装（自带重试）
#
# lark-cli 是飞书 API 的命令行客户端，所有飞书操作都通过它执行。
# --profile 指定使用哪个组织的凭证，--as user 以用户身份调用。
# ============================================================

def run_lark(args, *, profile, as_user=True, timeout=60, cwd=None):
    """执行 lark-cli 命令，自动处理限流和网络重试。

    Args:
        args: lark-cli 子命令和参数列表，如 ["drive", "+export", "--token", "xxx"]
        profile: lark-cli profile 名称（对应一个组织的认证凭证）
        as_user: 是否以用户身份调用（默认 True，否则以应用身份）
        timeout: 命令超时秒数
        cwd: 工作目录，默认 WORK_DIR

    Returns:
        命令的 stdout 字符串

    Raises:
        LarkPermissionError: 权限不足
        DocDeletedError: 文档已删除
        APIError: 其他 API 错误
        MigrationError: 超过最大重试次数
    """
    cmd = ["lark-cli"] + list(args) + ["--profile", profile]
    if as_user:
        cmd.extend(["--as", "user"])

    effective_cwd = cwd or WORK_DIR
    last_err = ""

    for attempt in range(MAX_NETWORK_RETRIES):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, cwd=effective_cwd)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            delay = NETWORK_RETRY_BASE * (2 ** min(attempt, 4))
            log(f"  TIMEOUT attempt {attempt+1}, retry in {delay}s")
            time.sleep(delay)
            continue

        if r.returncode == 0:
            return r.stdout

        combined = (r.stdout or "") + (r.stderr or "")
        last_err = combined

        # 限流：指数退避重试
        if _is_rate_limit(combined) and attempt < MAX_RETRIES - 1:
            delay = RATE_LIMIT_BASE * (2 ** attempt)
            log(f"  RATE_LIMIT attempt {attempt+1}, retry in {delay}s")
            time.sleep(delay)
            continue

        # 网络错误：更长的指数退避
        if _is_network_error(combined) and attempt < MAX_NETWORK_RETRIES - 1:
            delay = NETWORK_RETRY_BASE * (2 ** min(attempt, 4))
            log(f"  NETWORK attempt {attempt+1}, retry in {delay}s")
            time.sleep(delay)
            continue

        # 不可重试的错误：直接抛出对应异常
        if _is_permission_error(combined):
            raise LarkPermissionError(combined[:300])
        if _is_doc_deleted(combined):
            raise DocDeletedError(combined[:200])

        raise APIError(combined[:500])

    raise MigrationError(f"Max retries exceeded: {last_err[:200]}")


def run_lark_json(args, **kw):
    """执行 lark-cli 命令并将 stdout 解析为 JSON"""
    return parse_lark_json(run_lark(args, **kw))


def lark_api(method, path, *, params=None, data=None, file=None,
             profile, timeout=60, cwd=None):
    """通过 lark-cli api 子命令调用飞书 REST API。

    这是对 run_lark_json 的高层封装，自动处理参数序列化。
    示例: lark_api("GET", "/open-apis/docx/v1/documents/xxx/blocks", profile="seewo")
    等价于: lark-cli api GET /open-apis/docx/v1/documents/xxx/blocks --profile seewo --as user
    """
    a = ["api", method, path]
    if params:
        a.extend(["--params", json.dumps(params, ensure_ascii=False)])
    if data is not None:
        a.extend(["--data", json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)])
    if file:
        a.extend(["--file", file])
    return run_lark_json(a, profile=profile, timeout=timeout, cwd=cwd)

# ============================================================
# 状态管理
#
# state 文件是整个迁移的核心数据结构，记录了：
# - meta: 源/目标组织信息、profile 名称
# - nodes: 所有待迁移节点及其状态 (pending/done/failed/skipped)
# - node_mapping: 源 node_token → 目标 node_token 的映射
# - obj_mapping: 源 obj_token → 目标 obj_token 的映射
# - stats: 汇总统计
# ============================================================

def load_state(path):
    """读取 state JSON 文件"""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_state(path, state):
    """原子写入 state 文件，同时重新计算统计数据。

    使用 tmp + os.replace 保证写入的原子性——即使进程被中断，
    也不会出现写了一半的损坏文件。
    """
    state['meta']['updated_at'] = datetime.now().isoformat()
    ns = state['nodes']
    state['stats'] = {
        'total':   len(ns),
        'done':    sum(1 for n in ns if n['status'] == 'done'),
        'failed':  sum(1 for n in ns if n['status'] == 'failed'),
        'skipped': sum(1 for n in ns if n['status'] == 'skipped'),
        'pending': sum(1 for n in ns if n['status'] not in ('done', 'failed', 'skipped')),
    }
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def atomic_write_json(path, data):
    """原子写入任意 JSON 文件（不做 stats 计算），用于评论状态等辅助文件"""
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def set_node(state, node_token, **kw):
    """更新 state 中指定节点的字段（如 status、target_node_token 等）"""
    for n in state['nodes']:
        if n['node_token'] == node_token:
            n.update(kw)
            return
    raise ValueError(f"node {node_token} not found")


def find_node(state, node_token):
    """在 state 中查找指定 node_token 的节点，未找到返回 None"""
    for n in state['nodes']:
        if n['node_token'] == node_token:
            return n
    return None
