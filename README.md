# TsuyamaST Signage

## 方針（この構成が正）
- **GitHub / マスターUSB / 現地PC は同一構成**。
- **Python は2本のみ**。
  - 再生: `app/signagePC/auto_play.py`
  - PC管理: `app/signagePC/pc_agent.py`
    - 再起動／シャットダウン
    - 温度ログ
    - truncate
    - 容量監視

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
    hwinfo\
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
  - `runtime/hwinfo/HWiNFO64.exe`
