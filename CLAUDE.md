- PRを作成したら、直後に必ず `gh pr merge --auto --squash` を実行すること
  （Codexレビュー対象のPRでは下記ループで指摘ゼロを確認してから実行する）

## Codex 自動レビューのループ手順

このリポジトリでは PR を開くと OpenAI Codex（chatgpt-codex-connector）が自動でレビューコメントを付ける。
レビュー観点と重大度分類（P1/P2/P3）はルートの `AGENTS.md` に定義されている。

PR のレビュー対応は以下のループで行う：

1. PR を作成すると Codex の自動レビューが自動で走る（付くまで数分かかる）
2. GitHub MCP の `pull_request_read`（method: `get_review_comments`）でレビュースレッドを全件取得する
3. P1/P2 の指摘をすべて修正し、ローカルで動作確認してからブランチに push する
4. PR に「@codex review」とコメントして再レビューを依頼する
5. 指摘ゼロ（Codex がコメントなしで 👍 リアクションのみ返す）になるまで 2〜4 を繰り返す

注意：
- `gh` CLI が無い環境では GitHub MCP ツール（`enable_pr_auto_merge` / `merge_pull_request`）で代替する
- Codex の検出漏れはあり得る（実績：パストラバーサル欠陥を2回連続で見逃し）。
  既知の欠陥や自分で気づいた問題は、Codex に指摘されなくても修正する
