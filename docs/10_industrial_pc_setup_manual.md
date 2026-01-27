# 産業用PC セットアップ完全手順（サイネージ端末）

## 目的
- Windows産業用PCを「完全にゼロから」サイネージ端末としてセットアップする。
- `C:\_TsuyamaSignage` に配置した `auto_play.py` + `mpv` + embeddable Python により、無人で自動再生（フルスクリーン・無限ループ）できる状態にする。
- 作業者がコマンドを打たなくても運用できる状態を目指す。

## 必要物
- 産業用PC（Windows 11 Pro / Windows 10でもほぼ同様）
- 配布USB（`_TsuyamaSignage` 一式が入っているもの）
- 再生する動画ファイル（.mp4）
- キーボード・マウス・モニター

## 事前準備
- 管理者権限で作業できるユーザーでログインできる状態にする。
- USBから直接実行はしない（必ず `C:\_TsuyamaSignage` にコピーしてから実行）。

## 最終的なフォルダ構成（完成形）
```
C:\_TsuyamaSignage\
  runtime\
    python\  (embeddable python一式)
    mpv\     (mpv一式, mpv.exe含む)
  app\
    auto_play.py
    config\
      config.json
      active.json
    logs\
  content\
    ch01\
      *.mp4
    ch02\  ...
  start_signage.bat
```

## 手順

### 1. Windows初期セットアップ
1) 初回起動時の基本設定
- ローカルアカウント作成（組織方針によりMicrosoftアカウントは任意）
- 言語/キーボード/タイムゾーンを設定

2) 電源と画面の設定（無人端末用）
- **設定** → **システム** → **電源とバッテリー**
  - 画面オフ：**なし**
  - スリープ：**なし**
- **設定** → **システム** → **ディスプレイ**
  - 解像度：推奨
  - スケーリング：**100%（推奨）**

3) 更新/再起動についての運用注意
- 自動更新は完全停止ではなく、運用上の留意として「再起動が必要になる可能性がある」ことを関係者に周知する。

4) ネットワーク
- オフラインでも再生は可能。
- もしリモート管理や時刻同期が必要ならネット接続を設定。

### 2. アプリ配置（必須）
1) USBからフォルダをコピー
- 配布USB内の `_TsuyamaSignage` フォルダを **そのまま** `C:\_TsuyamaSignage` にコピーする。
- **注意：USBから直接実行しない。**

2) `start_signage.bat` を配置する
- 以下の内容を `C:\_TsuyamaSignage\start_signage.bat` として保存する（USB内の同名ファイルがあればそのまま使用）。

```bat
@echo off
cd /d C:\_TsuyamaSignage
set PATH=C:\_TsuyamaSignage\runtime\python;C:\_TsuyamaSignage\runtime\mpv;%PATH%
C:\_TsuyamaSignage\runtime\python\python.exe app\auto_play.py
```

3) 設定ファイル（テンプレート）
- `C:\_TsuyamaSignage\app\config\config.json`

```json
{
  "content_root": "C:/_TsuyamaSignage/content",
  "channels": ["ch01","ch02","ch03","ch04","ch05","ch06","ch07","ch08","ch09","ch10"],
  "fullscreen": true,
  "log_dir": "C:/_TsuyamaSignage/app/logs"
}
```

- `C:\_TsuyamaSignage\app\config\active.json`

```json
{ "active_channel": "ch01" }
```

4) 動画ファイル配置
- `C:\_TsuyamaSignage\content\ch01\` に `.mp4` を入れる。
- 再生順はファイル名の昇順（例：`01_intro.mp4`, `02_demo.mp4`）。
- ほかのチャンネルも使う場合は `ch02`, `ch03` を作成して配置する。

### 3. 自動起動設定（必須）
**基本は方法A（スタートアップ）。安定性重視なら方法B（タスクスケジューラ）。**

#### 方法A：スタートアップ（推奨・簡単）
1) `Win + R` → `shell:startup` を入力 → Enter
2) `C:\_TsuyamaSignage\start_signage.bat` のショートカットを作成して配置

#### 方法B：タスクスケジューラ（安定性重視）
1) `Win + R` → `taskschd.msc` → Enter
2) **タスクの作成** を選択
3) **全般**
- 名前：`TsuyamaSignage Auto Start`
- 「最上位の特権で実行する」にチェック
4) **トリガー**
- 「ログオン時」または「スタートアップ時」
5) **操作**
- プログラム/スクリプト：`C:\_TsuyamaSignage\start_signage.bat`
6) **条件/設定**
- 「失敗した場合、再起動する」などの復旧設定を有効化

## 完了確認
### 画面での確認（必須）
- 電源ON → 自動で再生が開始される。
- フルスクリーンで動画が連続再生される。

### コマンドでの確認（作業者が可能な場合）
PowerShell を開き、以下を実行：

```powershell
C:\_TsuyamaSignage\runtime\python\python.exe -V
mpv --version
```

ログ確認：
- `C:\_TsuyamaSignage\app\logs\auto_play.log` を開き、エラーがないこと

### 完了チェックリスト
- [ ] `C:\_TsuyamaSignage` にフォルダ一式がある
- [ ] `start_signage.bat` がある
- [ ] `config.json` と `active.json` がある
- [ ] `content\ch01` に mp4 がある
- [ ] 再起動後に自動再生される

## トラブルシュート
- **画面が真っ黒**
  - `content\ch01` に mp4 がない
  - `active.json` の `active_channel` が存在しないチャンネルになっている
  - `config.json` の `content_root` が間違っている

- **mpv が見つからない**
  - `C:\_TsuyamaSignage\runtime\mpv\mpv.exe` が存在するか確認
  - `start_signage.bat` の PATH が正しいか確認

- **起動してすぐ戻る/閉じる**
  - `C:\_TsuyamaSignage\app\logs\auto_play.log` を確認

- **ループ再生しない**
  - 対象チャンネルに mp4 があるか確認
  - ファイル名の順番が意図通りか確認

## 変更運用（必要時）
- チャンネル切替：`app\config\active.json` の `active_channel` を変更するだけ。
- コンテンツ差し替え：`content\chXX` の mp4 を差し替える。

## 注意事項
- **USBから直接実行しない。必ず `C:\_TsuyamaSignage` にコピーする。**
- `C:\_TsuyamaSignage` 以外に配置すると動作しない（`auto_play.py` が固定参照）。
