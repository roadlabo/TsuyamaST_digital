# TsuyamaST Signage

## 方針（この構成が正）
- **GitHub / マスターUSB / 現地PC は同一構成**。
- **Python は2本のみ**。
  - 再生: `app/signagePC/auto_play.py`
  - PC管理: `app/signagePC/pc_agent.py`
    - 再起動／シャットダウン
    - 死活監視／再生監視（heartbeat）
    - 負荷／容量監視

## フォルダ構成（GitHub/USB/現場で完全一致）
```
TsuyamaST_digital\
  app\
    analysisPC\
    config\
    signagePC\
  content\
  docs\
  logs\
  runtime\
    python\
    mpv\
```

## USB更新手順（GitHub → USB → 現場）
1. GitHub を clone して最新のリポジトリを取得する。
2. 取得したフォルダを **USBのルート** に `_TsuyamaSignage` として配置する。
3. 現地PCの `_TsuyamaSignage` を **USBの `_TsuyamaSignage` で上書き** する。
4. `_TsuyamaSignage` の構成が上記と一致していることを確認する。

## 重要な前提
- **GitHub / マスターUSB / 現地PC の構成は完全一致** させてください。
- runtime 配下には必ず以下を配置する。
  - `runtime/python/python.exe`
  - `runtime/mpv/mpv.exe`

## 動画同期の方式（ミラー同期）
- **全消去全転送ではありません**。差分のみコピーします。
- **マスターに無いファイルは削除**します（ADD/UPD/DEL のミラー同期）。
- **mtime/ctime/size が違う場合は差し換え**します（`compare_ctime` 設定で挙動を切替）。

## Controller 起動（タスクスケジューラ）

起動は C:\_TsuyamaSignage\start_controller.bat

venv固定：C:\_TsuyamaSignage\runtime\venv\Scripts\python(w).exe

ログ：C:\_TsuyamaSignage\logs\controller_start_*.log
