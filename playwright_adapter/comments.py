import random

from .activity_parser import clean_html_text, normalize_text
from .console import safe_print as print


DEFAULT_ROOT_COMMENT_LIMIT = 30
DEFAULT_HOT_COMMENT_LIMIT = 2
DEFAULT_REPLY_LIMIT_PER_COMMENT = 10
COMMENT_API_PAGE_SIZE = 20
MAX_ROOT_COMMENT_PAGES = 5


def extract_comment_author_name(comment):
    author_info = comment.get("author") or {}
    member_info = author_info.get("member") or {}
    return normalize_text(
        str(
            member_info.get("name")
            or author_info.get("name")
            or comment.get("author_name")
            or "匿名用户"
        )
    )


def parse_api_comment(comment):
    raw_content = comment.get("content") or comment.get("comment") or comment.get("text") or ""
    clean_content = clean_html_text(str(raw_content))
    if not clean_content or "已删除" in clean_content:
        return None
    reply_to = comment.get("reply_to_author") or {}
    reply_to_member = reply_to.get("member") or {}
    reply_to_name = normalize_text(
        str(reply_to_member.get("name") or reply_to.get("name") or "")
    )
    return {
        "id": str(comment.get("id") or ""),
        "author": extract_comment_author_name(comment),
        "content": clean_content,
        "vote_count": int(comment.get("vote_count") or 0),
        "reply_to": reply_to_name,
        "child_comment_count": int(comment.get("child_comment_count") or 0),
        "replies": [],
    }


def _comment_headers(page):
    return {
        "accept": "application/json, text/plain, */*",
        "x-requested-with": "fetch",
        "referer": page.url,
    }


def fetch_child_comments_via_api(page, root_comment_id, limit=DEFAULT_REPLY_LIMIT_PER_COMMENT):
    api_url = (
        f"https://www.zhihu.com/api/v4/comments/{root_comment_id}/child_comments"
        f"?limit={limit}&offset=0"
    )
    response = page.context.request.get(api_url, headers=_comment_headers(page))
    if not response.ok:
        response_text = response.text()[:200]
        raise RuntimeError(f"评论回复 API 请求失败: HTTP {response.status}, body={response_text}")
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError(f"评论回复 API 返回结构异常: keys={list(payload.keys())}")
    replies = [parsed for item in data if (parsed := parse_api_comment(item))]
    return replies[:limit]


def fetch_comments_via_api(
    page,
    answer_id,
    limit=DEFAULT_ROOT_COMMENT_LIMIT,
    hot_comment_limit=DEFAULT_HOT_COMMENT_LIMIT,
    reply_limit=DEFAULT_REPLY_LIMIT_PER_COMMENT,
):
    comments = []
    source_items = []
    seen_ids = set()
    raw_count = 0
    offset = 0

    for page_number in range(MAX_ROOT_COMMENT_PAGES):
        if len(comments) >= limit:
            break
        page_limit = min(COMMENT_API_PAGE_SIZE, limit - len(comments))
        api_url = (
            f"https://www.zhihu.com/api/v4/answers/{answer_id}/root_comments"
            f"?limit={page_limit}&offset={offset}&order=normal&status=open"
        )
        response = page.context.request.get(api_url, headers=_comment_headers(page))
        if not response.ok:
            response_text = response.text()[:200]
            if page_number == 0:
                raise RuntimeError(
                    f"评论 API 请求失败: HTTP {response.status}, body={response_text}"
                )
            print(
                f"   ⚠️ 顶层评论第 {page_number + 1} 页请求失败，"
                f"保留已获取的 {len(comments)} 条: HTTP {response.status}"
            )
            break
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list):
            if page_number == 0:
                raise RuntimeError(f"评论 API 返回结构异常: keys={list(payload.keys())}")
            print(f"   ⚠️ 顶层评论第 {page_number + 1} 页结构异常，停止分页。")
            break
        if not data:
            break
        raw_count += len(data)
        offset += len(data)
        for item in data:
            parsed = parse_api_comment(item)
            comment_id = parsed["id"] if parsed else ""
            if parsed and (not comment_id or comment_id not in seen_ids):
                comments.append(parsed)
                source_items.append(item)
                if comment_id:
                    seen_ids.add(comment_id)
            if len(comments) >= limit:
                break
        if len(data) < page_limit:
            break
        if len(comments) < limit:
            page.wait_for_timeout(random.randint(300, 800))

    for index, (comment, source_item) in enumerate(
        zip(comments[:hot_comment_limit], source_items[:hot_comment_limit]),
        start=1,
    ):
        child_count = comment["child_comment_count"]
        if child_count <= 0 or not comment["id"]:
            continue
        embedded = [
            parsed
            for child in (source_item.get("child_comments") or [])
            if (parsed := parse_api_comment(child))
        ][:reply_limit]
        expected_count = min(child_count, reply_limit)
        if len(embedded) >= expected_count:
            comment["replies"] = embedded
            continue
        try:
            page.wait_for_timeout(random.randint(300, 800))
            comment["replies"] = fetch_child_comments_via_api(
                page,
                comment["id"],
                limit=reply_limit,
            )
        except Exception as exc:
            comment["replies"] = embedded
            print(
                f"   ⚠️ 第 {index} 条热门评论的回复提取失败，"
                f"保留 {len(embedded)} 条内嵌回复: {str(exc)[:160]}"
            )
    reply_count = sum(len(comment["replies"]) for comment in comments)
    print(
        f"   ✅ 评论 API 成功，分页原始数量 {raw_count}，"
        f"有效顶层评论 {len(comments)}，热门评论回复 {reply_count}"
    )
    return comments


def fetch_first_page_comments_via_api(*args, **kwargs):
    """Backward-compatible name retained for callers outside the package."""
    return fetch_comments_via_api(*args, **kwargs)


def format_comments_markdown(comments):
    if not comments:
        return ""
    lines = ["", "", "---", f"### 💬 精选评论（最多 {DEFAULT_ROOT_COMMENT_LIMIT} 条）", ""]
    for comment in comments:
        content = comment["content"].replace("\n", "\n> ")
        lines.append(f"> **{comment['author']}**：{content}")
        for reply in comment.get("replies") or []:
            reply_content = reply["content"].replace("\n", "\n>> ")
            reply_target = f" 回复 **{reply['reply_to']}**" if reply.get("reply_to") else ""
            lines.append(f">> ↳ **{reply['author']}**{reply_target}：{reply_content}")
            lines.append(">>")
        lines.append(">")
    return "\n".join(lines)
