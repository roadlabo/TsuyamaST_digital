# TsuyamaST Signage

## 方針（この構成が正）
- **GitHub / マスターUSB / 現地PC は同一構成**。
- **power_agent は捨てて正解**。リポジトリ内に存在しません。
- **Python は2本のみ**。
  - 再生: `app/player/auto_play.py`
  - PC管理: `app/pc_agent.py`
    - 再起動／シャットダウン
    - 温度ログ
    - truncate
    - 容量監視

## フォルダ構成（C:\_TsuyamaSignage）
```
C:\_TsuyamaSignage\
  app\
  runtime\
  logs\
  signage\
  docs\
```

## USB更新手順（GitHub → USB → 現場）
1. GitHub を clone して最新のリポジトリを取得する。
2. 取得したフォルダを **USBのルート** に `_TsuyamaSignage` として配置する。
3. 現地PCの `C:\_TsuyamaSignage` を **USBの `_TsuyamaSignage` で上書き** する。
4. `C:\_TsuyamaSignage` の構成が上記と一致していることを確認する。

## 重要な前提
- すべての説明・パスは `C:\_TsuyamaSignage` を前提に統一しています。
- **GitHub / マスターUSB / 現地PC の構成は完全一致** させてください。
