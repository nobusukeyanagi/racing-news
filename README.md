# 公営競技ニュース

Yahoo! JAPAN「フォロー」の公営競技5テーマを取得し、競技別に最新100件まで保持して表示します。

## 対象
- 競輪
- オートレース
- ボートレース
- 地方競馬
- JRA

## 更新
日本時間 0:00 / 6:00 / 12:00 / 18:00 頃にGitHub Actionsを起動します。
GitHub Actionsの仕様上、実行開始が遅れる場合があります。

Yahoo!フォローのテーマページは一度に100件すべてが静的HTMLへ出ないため、各回の取得結果を前回スナップショットへ追加し、各競技の最新100件を保持します。初回はテーマページから取得できた件数から始まり、更新を重ねることで最大100件まで蓄積します。

## 表示
- 左：競技切替＋タイトル一覧
- タイトル一覧はPCで最大25文字。26文字以上は末尾を「…」にします。SPでは画面幅に合わせてCSSで省略します
- 右：ニュース本文
- NEW表示・NEWのみ表示・スマホのタイトル一覧メニュー・下引き更新は「最新ニュース64」に準拠
- 競技切替時は左右とも同じ競技だけを表示
- 本文や画像が取得できない記事は元記事リンクだけ残します

## GitHub Pages
1. ZIPの中身を `Racing-news` リポジトリ直下へアップロード
2. Settings → Pages → Source を `GitHub Actions` に変更
3. Actions → `Update racing news` → Run workflow
4. 完了後にPages URLを確認

外部APIキーは不要です。

## 取得元テーマ
- 競輪: https://follow.yahoo.co.jp/themes/0bef3418cd3e109f7707/
- オートレース: https://follow.yahoo.co.jp/themes/0225b58b93568d981167/
- ボートレース: https://follow.yahoo.co.jp/themes/08a589647e213a249875/
- 地方競馬: https://follow.yahoo.co.jp/themes/0dd1a07d77824fcd0458/
- JRA: https://follow.yahoo.co.jp/themes/03ebffae693d76102412/

ページはnoindexです。記事本文・画像はGitへコミットせず、Actionsキャッシュに保持します。

## v02表示仕様

- 緑色は `#14532d`
- PCの競技選択はタイトル右にテキスト表示。選択中のみ緑、その他はグレー
- SPの競技選択はハンバーガー内の先頭
- 左タイトル一覧は各競技最大100件。NEWは緑、それ以外は青
- ニュースタイトルは緑
- 見出し末尾の括弧書き、本文末尾の執筆者名・執筆社名を表示時に除去


## Discord通知

GitHubの `Settings → Secrets and variables → Actions` に、
`DISCORD_WEBHOOK_URL` という名前でDiscord Webhook URLを登録してください。

ニュース取得・Pages公開が成功した更新時に、以下を送信します。

```text
🐴 公営競技ニュースを更新しました
https://nobusukeyanagi.github.io/racing-news/
```

デザイン変更など、保存済みデータからページだけを再生成した場合は通知しません。
