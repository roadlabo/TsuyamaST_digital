# インストールUSB 再作成手順

## 目的
- インストール用USBが紛失した場合に、別PC（ネット接続あり）から **1から作り直す**。
- 作業者が手順に迷わず、`_TsuyamaSignage` を再構成できるUSBを作成する。

## 必要物
- ネット接続のある作成用PC（Windows 11 / 10）
- USBメモリ（十分な空き容量）
- 既存の `auto_play.py`（このリポジトリまたはバックアップから取得）

## 事前準備
- 作成用PCに新規フォルダを作る（例：`D:\_TsuyamaSignage`）。
- **注意：USBから直接実行しない。** 完成後は現地PCの `_TsuyamaSignage` にコピーして使う。

## 最終的なフォルダ構成（完成形）
```
_TsuyamaSignage\
  app\
    config\
      Sign01\
        config.json
        active.json
      Sign02\ ...
    signagePC\
      auto_play.py
      pc_agent.py
      start_auto_play.bat
      start_pc_agent.bat
      requirements_controller.txt
  content\
  docs\
  logs\
  runtime\
    python\  (embeddable python一式)
    mpv\     (mpv一式, mpv.exe含む)
    hwinfo\  (HWiNFO64.exe含む)
```

## 手順

### 1. 作業フォルダを作成
- 例：`D:\_TsuyamaSignage` を作成する。
- 以降、完成したフォルダ `_TsuyamaSignage` をUSBのルートにコピーする。

### 2. embeddable Python を入手・配置
1) Python公式サイトの **Downloads** にアクセス
2) **Windows embeddable package (64-bit)** をダウンロード
3) 展開して `runtime\python` に配置

#### _pth 設定（必須）
`runtime\python\python3*. _pth` を開いて以下の内容にする：

```
python314.zip
.
Lib
Lib\site-packages
import site
```

- バージョン表記（`python314.zip`）は **ダウンロードしたバージョンに合わせる**。

#### site-packages フォルダ作成
- `runtime\python\Lib\site-packages` を作成する（空でOK）。

### 3. mpv を入手・配置
1) **SourceForge の mpv-player-windows** から最新版を取得
2) 展開して `runtime\mpv` に配置
- `runtime\mpv\mpv.exe` があることを確認する

### 4. アプリファイルを配置
- `app\signagePC\auto_play.py` を配置する（このリポジトリから取得）
- `app\config\SignXX` フォルダに `config.json` と `active.json` を配置する

#### config.json テンプレート
`app\config\SignXX\config.json`:

```json
{
  "content_root": "content",
  "channels": ["ch01","ch02","ch03","ch04","ch05","ch06","ch07","ch08","ch09","ch10"],
  "fullscreen": true
}
```

#### active.json テンプレート
`app\config\SignXX\active.json`:

```json
{ "active_channel": "ch01" }
```

#### start_auto_play.bat
`app\signagePC\start_auto_play.bat` をそのまま利用：

```bat
@echo off
"C:\_TsuyamaSignage\runtime\python\python.exe" "C:\_TsuyamaSignage\app\signagePC\auto_play.py"
```

### 5. コンテンツを配置
- `content\ch01` を作成し、`.mp4` を入れる。
- 他チャンネルを使う場合は `content\ch02`, `content\ch03` を作成。

### 6. USBへコピー
- 完成した `_TsuyamaSignage` フォルダをUSBのルートにコピーする。

## 完了確認
### USB内チェックリスト
- [ ] `runtime\python` に embeddable python 一式がある
- [ ] `runtime\mpv` に mpv 一式がある
- [ ] `runtime\hwinfo` に HWiNFO64.exe がある
- [ ] `app\signagePC\auto_play.py` がある
- [ ] `app\config\SignXX\config.json` がある
- [ ] `app\config\SignXX\active.json` がある
- [ ] `content\ch01` に mp4 がある
- [ ] `app\signagePC\start_auto_play.bat` がある

### 作成用PCでの簡易検証（可能な場合）
PowerShell で以下を実行：

```powershell
D:\_TsuyamaSignage\runtime\python\python.exe -V
D:\_TsuyamaSignage\runtime\mpv\mpv.exe --version
```

- **注意：USBから直接実行しない。本番は `_TsuyamaSignage` にコピーして動作させる。**

## トラブルシュート
- **python.exe が動かない**
  - `_pth` の内容が正しいか確認
  - `Lib\site-packages` が存在するか確認

- **mpv.exe が見つからない**
  - `runtime\mpv\mpv.exe` の位置を確認

- **config/active が読み込まれない**
  - `app\config\SignXX` 配下にあるか確認
  - JSONの形式が崩れていないか確認

## 変更運用（必要時）
- チャンネル切替：`active.json` の `active_channel` を変更するだけ。
- コンテンツ差し替え：`content\chXX` の mp4 を差し替える。

## 注意事項
- **USBから直接実行しない。必ず `_TsuyamaSignage` にコピーして使う。**
