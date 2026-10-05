#!/usr/bin/env bash
# 已发布版本永久撤回端到端示例：
#   上传 -> 标注 -> 单份发布 v1/v2 -> 撤回 v1（非空原因）
#   -> 查询撤回时间与原因 -> 重复撤回返回首次记录（200，不改写原因）
#   -> 空原因拒绝（400）-> 撤回后公开元数据/图片读取 410，未撤回版本仍可读
#   -> 文档详情/发布列表/批次清单标识撤回状态 -> 撤回不删记录、版本号不复用
#   -> 批次幂等键重放仍返回首次响应，但不恢复撤回版本的公开访问
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/wd_doc_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/wd_doc_b.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

req() { python3 -m json.tool; }  # 管道美化

echo "== 1. 上传两份原图并标注（A -> rev1，B 保持 rev0）=="
A=$(upload /tmp/wd_doc_a.png)
B=$(upload /tmp/wd_doc_b.png)
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
echo "A=$A  B=$B"

echo "== 2. A 单份发布 v1，随后发布 v2 =="
curl -sS -X POST "$BASE/documents/$A/publish" | req
curl -sS -X POST "$BASE/documents/$A/publish" | req

echo "== 3. 永久撤回 A v1（首次：HTTP 201，返回撤回时间与原因）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$A/publications/1/withdrawal" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"发布后复核发现一处遮盖遗漏，永久撤回"}'

echo "== 4. 查询撤回时间与原因 =="
curl -sS -w "\nHTTP %{http_code}\n" \
  "$BASE/documents/$A/publications/1/withdrawal"

echo "== 5. 重复撤回（不同原因 / 空原因）：HTTP 200 返回首次记录，不改写原因 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$A/publications/1/withdrawal" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"试图改写原因"}'
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$A/publications/1/withdrawal" \
  -H 'Content-Type: application/json' \
  -d '{"reason":"   "}'

echo "== 6. 未撤回版本带空原因撤回 -> 400 reason_required（演示拒绝）=="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$A/publications/2/withdrawal" \
  -H 'Content-Type: application/json' \
  -d '{"reason":""}'

echo "== 7. 撤回后新发起的公开读取 -> 410 version_withdrawn（不给成品与遮盖详情）=="
curl -sS -w "\nHTTP %{http_code}\n" "$BASE/publications/$A/1"
curl -sS -o /dev/null -w "image HTTP %{http_code}\n" "$BASE/publications/$A/1/image"
echo "未撤回的 v2 仍按原规则公开："
curl -sS -o /dev/null -w "v2 metadata HTTP %{http_code}\n" "$BASE/publications/$A/2"
curl -sS -o /dev/null -w "v2 image    HTTP %{http_code}\n" "$BASE/publications/$A/2/image"

echo "== 8. 未知文档/版本沿用既有错误 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST \
  "$BASE/documents/$A/publications/99/withdrawal" \
  -H 'Content-Type: application/json' -d '{"reason":"x"}'

echo "== 9. 文档详情与发布列表：原字段与顺序不变，末尾标识撤回状态 =="
curl -sS "$BASE/documents/$A" | req

echo "== 10. 批次清单也标识撤回状态（B 批次发布后，连带 A 再发一版）=="
KEY="wd-demo-$(date +%s)"
RESP=$(curl -sS -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY\",\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}")
echo "$RESP" | req
BATCH_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")
curl -sS "$BASE/publication-batches/$BATCH_ID" | req

echo "== 11. 幂等键重放：仍返回首次保存的响应（HTTP 200），但不恢复 A v1 的公开访问 =="
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"idempotency_key\":\"$KEY\",\"documents\":[
    {\"doc_id\":\"$A\",\"expected_revision\":1},
    {\"doc_id\":\"$B\",\"expected_revision\":0}
  ]}" -o /dev/null
curl -sS -o /dev/null -w "A v1 公开读取仍为 HTTP %{http_code}\n" "$BASE/publications/$A/1"

echo "== 12. 撤回不删发布记录、不复用版本号：A 现有版本序列 =="
curl -sS "$BASE/documents/$A/publications" | python3 -c "
import sys, json
for p in json.load(sys.stdin)['publications']:
    print('v%s withdrawn=%s withdrawn_at=%s'
          % (p['version'], p['withdrawn'], p['withdrawn_at']))
"
