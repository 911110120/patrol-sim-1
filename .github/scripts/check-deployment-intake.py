import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


COMMENT_MARKER = "<!-- patrol-sim-deployment-intake -->"
FIELDS = ("小组或个人", "完成内容", "镜像", "容器端口", "环境变量名称", "验证路径")
REPOSITORY_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
IMAGE_REFERENCE = re.compile(
    r"(?P<registry>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?)/"
    rf"(?P<repository>{REPOSITORY_COMPONENT}(?:/{REPOSITORY_COMPONENT})*)"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"@(?P<digest>sha256:[a-f0-9]{64})"
)
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
ENV_NAMES = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[\s,，]+[A-Za-z_][A-Za-z0-9_]*)*")
GHCR = "https://ghcr.io"
MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def request_json(url, *, token=None, accept="application/json", method="GET", body=None):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        url,
        data=payload,
        method=method,
        headers={"Accept": accept, "Content-Type": "application/json"},
    )
    if token:
        # GHCR may redirect config blobs to its CDN. Keep credentials on the original request.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def validate_fields(body):
    # Ignore the template's commented examples.
    text = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    fields = {}
    errors = []
    for field in FIELDS:
        values = re.findall(rf"^[ \t]*[-*][ \t]+{field}[ \t]*[:：]([^\r\n]*)", text, re.MULTILINE)
        if len(values) != 1:
            errors.append(f"请按模板填写一行「{field}」。")
            continue
        value = values[0].strip().strip("`").strip()
        if not value or value.lower() in {"xxx", "待填写", "待补充"}:
            errors.append(f"请补全「{field}」。")
            continue
        fields[field] = value

    image = IMAGE_REFERENCE.fullmatch(fields.get("镜像", ""))
    if "镜像" in fields and not image:
        errors.append("镜像请填完整仓库地址和 SHA-256 摘要，例如 `ghcr.io/owner/image@sha256:…`。")

    port = fields.get("容器端口")
    if port and (not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535):
        errors.append("容器端口请填写 1 到 65535 之间的端口号，例如 `8000`。")

    env_names = fields.get("环境变量名称")
    if env_names and env_names != "无" and not ENV_NAMES.fullmatch(env_names):
        errors.append("环境变量请只写变量名，以逗号或空格分隔。没有环境变量时填「无」。")

    path = fields.get("验证路径")
    if path and (
        not path.startswith("/") or path.startswith("//")
        or any(char.isspace() or char in "\\?#" for char in path)
    ):
        errors.append("验证路径请填写站内路径，例如 `/` 或 `/api/health`。")
    return errors, image


def check_ghcr_platform(repository, digest):
    query = urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    token = request_json(f"{GHCR}/token?{query}")["token"]
    image_url = f"{GHCR}/v2/{repository}"
    manifest = request_json(f"{image_url}/manifests/{digest}", token=token, accept=MANIFEST_TYPES)
    if "manifests" in manifest:
        platforms = [entry.get("platform") or {} for entry in manifest["manifests"]]
    else:
        config_digest = manifest.get("config", {}).get("digest", "")
        if not DIGEST.fullmatch(config_digest):
            return False
        platforms = [request_json(f"{image_url}/blobs/{config_digest}", token=token)]
    return any(p.get("os") == "linux" and p.get("architecture") == "amd64" for p in platforms)


def check_submission(body):
    errors, image = validate_fields(body)
    if errors:
        return "待补充", errors

    if image["registry"] != "ghcr.io":
        return "待工作组确认", ["请工作组核对该镜像仓库的公开访问和 `linux/amd64` 支持。"]

    try:
        supported = check_ghcr_platform(image["repository"], image["digest"])
    except HTTPError as error:
        if error.code in {401, 403, 404}:
            return "待补充", ["GHCR 上未能匿名读取该镜像摘要。请确认镜像为 Public，摘要与发布记录一致。"]
        return "暂未完成", ["GHCR 暂时无法核验。请稍后编辑 PR 正文触发重试。"]
    except (URLError, TimeoutError, ValueError, KeyError):
        return "暂未完成", ["GHCR 暂时无法核验。请稍后编辑 PR 正文触发重试。"]

    if not supported:
        return "待补充", ["该摘要未提供 `linux/amd64` 镜像。请重新发布并更新 PR 中的摘要。"]
    return "通过", [
        "报名字段齐全，GHCR 镜像摘要可匿名访问，支持 `linux/amd64`。",
        "PR 保持 Open，等工作组配置好 Coolify 应用并发出邀请后再部署。",
    ]


def update_comment(api, pr_number, text):
    endpoint = f"/issues/{pr_number}/comments"
    page = 1
    while True:
        comments = api(f"{endpoint}?per_page=100&page={page}")
        for comment in comments:
            if comment["user"]["login"] == "github-actions[bot]" and comment["body"].startswith(COMMENT_MARKER):
                if comment["body"] != text:
                    api(f"/issues/comments/{comment['id']}", method="PATCH", body={"body": text})
                return
        if len(comments) < 100:
            break
        page += 1
    api(endpoint, method="POST", body={"body": text})


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pr_number = int(event["number"])
    api_base = f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"
    token = os.environ["GITHUB_TOKEN"]

    def api(path, **kwargs):
        return request_json(f"{api_base}{path}", token=token, accept="application/vnd.github+json", **kwargs)

    pr_endpoint = f"/pulls/{pr_number}"
    pr = api(pr_endpoint)
    if pr["state"] != "open" or pr["draft"] or pr["base"]["ref"] != "main":
        return 0

    status, notes = check_submission(pr["body"] or "")
    latest = api(pr_endpoint)
    if any(latest[key] != pr[key] for key in ("body", "state", "draft")) or latest["base"]["ref"] != "main":
        return 0

    comment = "\n".join([
        COMMENT_MARKER, f"### 部署报名检查 · {status}", "",
        *(f"- {note}" for note in notes), "",
        "本检查范围是报名信息和镜像元数据。项目功能由学员部署后验证。",
    ])
    update_comment(api, pr_number, comment)
    print(f"部署报名检查 · {status}")
    return 1 if status in {"待补充", "暂未完成"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
