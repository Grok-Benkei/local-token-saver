# local-token-saver (`token_saver.py`)

**外部通信ゼロ・外部パッケージ依存ゼロ（Python標準ライブラリ＋SQLiteのみ）で、AIエージェント（Cursor / Claude Code / Grok Bot 等）のトークン消費を最大74〜99.6%削減する単一ファイル・ツールキット**

---

## なぜこれを作ったのか？（背景）

AIエージェントのトークン削減ツールとして有名なOSS群をコード監査したところ、以下のようなリスクやオーバーヘッドが確認されました。

- **サプライチェーン攻撃のリスク**：多数のAIツールが依存するパッケージに `.pth` マルウェアが混入する事案が発生。
- **デフォルトONのテレメトリ通信**：一部の記憶系・管理系パッケージで、初期設定のまま外部（PostHog / Amplitude等）へテレメトリを送信する実装が存在。
- **過剰なリソース消費**：トークンを削るためだけに数GBのPyTorchモデルやヘッドレスブラウザを起動すると、かえって遅くなりコストが増加。

そこで、**「本当にトークン削減に効く4つの核心機能」だけを、Python標準ライブラリ（`sqlite3`, `html.parser`, `re`, `hashlib`, `subprocess`）のみ・単一ファイル（約300行）で実装**しました。`pip install` は一切不要です。

---

## 4つの核心機能と実測削減率

1. **CLI・テスト・ビルド出力の自動圧縮（`run`）**
   - 大量の `PASS` / `PASSED` 行を1行のサマリに畳み、進捗バーやANSIエスケープ、同一タイムスタンプの重複ログ（`[repeated xN]`）を圧縮しつつ、**`FAIL`・エラー・スタックトレースは100%無傷で保持**します。
   - **実測削減率**：`1,842トークン ➔ 52トークン`（**97.2% 削減**）
2. **生HTMLから不要タグを剥がすコンパクトMarkdown変換（`html2md`）**
   - `<script>`, `<style>`, `<svg>`, `<nav>`, `<footer>`, `<iframe>` を標準パーサーで即座に除去し、見出し・リンク・本文だけの軽量Markdownに変換します。
   - **実測削減率**：EC検索HTML `308,854 ➔ 19,640トークン`（**93.6% 削減**）、案件詳細HTML `25,320 ➔ 91トークン`（**99.6% 削減**）
3. **SQLite（WALモード）による完全一致＋準一致キャッシュ（`LocalStateStore`）**
   - 外部ベクトルDBを使わず、SHA-256による完全一致と正規化トークンJaccard類似度による準一致（表記ゆれ・語順違いの吸収）をミリ秒単位で判定し、再取得トークンをゼロにします。
4. **SQLite永続化サーキットブレーカー（`breaker_allow` / `breaker_record`）**
   - 外部APIや取得先が連続でエラー（429レート制限や500エラー等）を返した際、自動で回路を `OPEN`（遮断）にしてクールダウンさせ、AIエージェントの無限リトライによるトークン浪費を物理的に止めます。

---

## クイックスタート（インストール不要）

```bash
# 1. セルフテスト＆ベンチマークの実行（-I で外部site-packagesを完全遮断して実行可能）
python3 -I token_saver.py selftest

# 2. テストやビルド等の長いCLI出力を自動圧縮して実行
python3 -I token_saver.py run pytest -v

# 3. ローカルHTMLファイル（または標準入力）を軽量Markdownに変換
python3 -I token_saver.py html2md page.html
curl -sL https://example.com | python3 -I token_saver.py html2md
```

### Pythonスクリプトからの呼び出し例

```python
from token_saver import LocalStateStore, compress_cli_output, html_to_compact_markdown, chunk_markdown

store = LocalStateStore()

# キャッシュ確認（完全一致＋類似クエリ判定）
val, status = store.cache_get("検索クエリやURL")
if val is None:
    # サーキットブレーカー確認（3回連続失敗で自動クールダウン遮断）
    allowed, reason = store.breaker_allow("api_service_name")
    if not allowed:
        raise RuntimeError(f"Blocked by circuit breaker: {reason}")
    try:
        raw_html = "<html>...</html>"
        val = html_to_compact_markdown(raw_html)
        store.breaker_record("api_service_name", success=True)
        store.cache_put("検索クエリやURL", val, ttl_sec=3600)
    except Exception:
        store.breaker_record("api_service_name", success=False)
        raise
```

---

## ライセンス

MIT License
