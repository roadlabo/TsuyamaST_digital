# PowerAgent Manual

## 目的
現地サイネージPCで `command.json` を監視し、再起動/シャットダウンを実行するエージェントです。

## 配置パス
```
C:\_TsuyamaSignage\
├─ app\
│  ├─ agent\
│  │  ├─ power_agent.py
│  │  └─ run_power_agent.bat
│  ├─ config\
│  │  ├─ command.json
│  │  ├─ command_result.json
│  │  └─ power_agent_state.json
│  └─ logs\
└─ ...
```

## 起動方法
- `run_power_agent.bat` を実行します。
- Python 3.11 以上を想定しています。

## コマンド仕様
`command.json` に以下の形式で書き込みます。

```
{
  "command_id": "20260131_111111_Sign01",
  "command": "reboot",
  "issued_at": "2026-01-31 11:11:11"
}
```

PowerAgent は `command_id` の重複実行を防ぐため `power_agent_state.json` を保持します。

## 実行結果
`command_result.json` に以下の形式で結果を書き戻します。

```
{
  "command_id": "20260131_111111_Sign01",
  "status": "ok",
  "finished_at": "2026-01-31 11:11:20",
  "message": ""
}
```

## ログ
`app/logs/power_agent_YYYYMMDD.log` に実行履歴を記録します。

## タスクスケジューラ登録手順
1. Windows の「タスク スケジューラ」を起動します。
2. 「タスクの作成」をクリックします。
3. 全般タブ
   - 名前: `TsuyamaST_PowerAgent`
   - 「最上位の特権で実行する」にチェック
4. トリガータブ
   - 「新規」→「ログオン時」または「起動時」を選択
5. 操作タブ
   - 「新規」→「プログラムの開始」
   - プログラム/スクリプト: `C:\_TsuyamaSignage\app\agent\run_power_agent.bat`
6. 条件/設定タブ
   - 必要に応じて「AC 電源」など環境に合わせて調整します。
7. 保存後、手動で実行してログが出力されることを確認してください。
