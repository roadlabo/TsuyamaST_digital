# 津山駅サイネージ
# 産業用PC セットアップマニュアル【初心者向け・端的・BAT起動統一版】

## 冒頭に必ず確認すること
- 管理者ユーザー：**SystemAV**
- 管理者ログオンパスワード：**kanrisya**
- この手順は**上から順にそのまま実行する。**
- **ファイル名・フォルダ名は絶対に変更しない。**

---

## 作業全体の流れ（この順番で固定）
1. ディスプレイの向き（縦）を確定
2. 壁紙（サイネージ番号）を設定
3. USBから C:\_TsuyamaSignage をコピー
4. 画面が消えない設定（スリープ無効）
5. WindowsファイアーウォールをOFF
6. フォルダ共有（config / logs / content）
7. IPアドレス固定
8. 自動ログイン設定（SystemAV / kanrisya）
9. タスクスケジューラ設定（本番＝BAT起動）
10. 再起動して自動再生を確認

---

## 変更しない事項（必ず守る）
- C:\_TsuyamaSignage フォルダ構成
- 共有フォルダ名（config / logs / content）
- BATファイル名（start_auto_play.bat / start_pc_agent.bat）

---

## 1. ディスプレイの向き（縦）を確定
**操作不能防止のため、最初に実施する。**

1. デスクトップの何もないところで右クリック
2. 「ディスプレイ設定」をクリック
3. 「画面の向き」を探す
4. **縦 / 縦（反対向き）** を切り替え、正しい向きにする
5. 「変更を保持する」

---

## 2. 壁紙（サイネージ番号）を設定
**サイネージ番号＝IPアドレス下2桁。必ず一致させる。**

1. デスクトップ右クリック → 「個人用設定」
2. 「背景」 → 「写真を参照」
3. USB内の壁紙フォルダから番号画像を選ぶ
   - 例：01番 → `signage_01.png`
4. 番号が大きく表示されればOK

---

## 3. USBから C:\_TsuyamaSignage をコピー
1. USBメモリを挿す
2. エクスプローラーを開く
3. USB内の **_TsuyamaSignage** を右クリック → コピー
4. Cドライブ直下に貼り付け
5. `C:\_TsuyamaSignage` が存在すればOK

**注意**
- フォルダ名は変更しない
- Cドライブ直下に置く

---

## 4. 画面が消えない設定（スリープ無効）
1. 設定 → システム → 電源とバッテリー
2. 「画面とスリープ」の全項目を **なし** にする

---

## 5. WindowsファイアーウォールをOFF
1. Windows セキュリティ → 「ファイアーウォールとネットワーク保護」
2. 以下3つをすべてOFF
   - ドメインネットワーク
   - プライベートネットワーク
   - パブリックネットワーク
3. すべて「オフ」と表示されればOK

---

## 6. フォルダ共有（config / logs / content）
対象フォルダ：
- `C:\_TsuyamaSignage\app\config`
- `C:\_TsuyamaSignage\logs`
- `C:\_TsuyamaSignage\content`

各フォルダで共通操作：
1. フォルダ右クリック → 「アクセスを許可する」 → 「特定のユーザー」
2. 「Everyone」を追加
3. 権限を「読み取り/書き込み」に変更
4. 「共有」をクリック

確認：
- `\\localhost\config` / `\\localhost\logs` / `\\localhost\content` が開ける

---

## 7. IPアドレス固定
**壁紙番号と必ず一致。**

- 範囲：`192.168.1.201 ～ 192.168.1.220`
- 下2桁＝サイネージ番号
- サブネットマスク：`255.255.255.0`

手順：
1. 設定 → ネットワークとインターネット → イーサネット
2. 「IP割り当て」→ 編集 → 手動 → IPv4 ON
3. IPアドレス / サブネットマスクを入力 → 保存
4. `ipconfig` で確認

---

## 8. 自動ログイン設定（SystemAV / kanrisya）
**管理者ユーザー：SystemAV**
**管理者ログオンパスワード：kanrisya**

### 8-1 管理者パスワード設定
1. SystemAVでログイン
2. 設定 → アカウント → サインイン オプション
3. 「パスワード」→「追加」
4. 新しいパスワード：`kanrisya`

### 8-2 自動ログイン（レジストリ）
1. `Windows + R` → `regedit` → OK
2. 以下を開く
```
HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon
```
3. 文字列値を設定（3つだけ）
   - `AutoAdminLogon` = `1`
   - `DefaultUserName` = `SystemAV`
   - `DefaultPassword` = `kanrisya`
4. 再起動してログイン画面が出なければOK

---

## 9. タスクスケジューラ設定（本番＝BAT起動）
**方針（必ず守る）**
タスクスケジューラが起動するのは **.bat と .exe のみ。**
Python のことは気にしなくてよい。

### 9-1 作成するタスクは3つだけ
| タスク名 | 起動するもの | トリガー | 遅延 |
|---|---|---|---|
| Tsuyama HWiNFO Auto Start | HWiNFO64.exe | 起動時 | 30秒 |
| Tsuyama PC Agent Auto Start | start_pc_agent.bat | 起動時 | 2分 |
| Tsuyama Auto Play Start | start_auto_play.bat | ログオン時（SystemAV） | 30秒 |

### 9-2 共通ルール
- **必ず .bat / .exe を指定する**
- **開始（オプション）は同フォルダを指定**
- タスク作成時にパスワードを求められたら **kanrisya** を入力

### 9-3 各タスクの指定内容

#### A. Tsuyama HWiNFO Auto Start
- **プログラム/スクリプト：**
  `C:\_TsuyamaSignage\runtime\hwinfo\HWiNFO64.exe`
- **開始（オプション）：**
  `C:\_TsuyamaSignage\runtime\hwinfo`
- **トリガー：** 起動時
- **遅延：** 30秒

#### B. Tsuyama PC Agent Auto Start
- **プログラム/スクリプト：**
  `C:\_TsuyamaSignage\app\signagePC\start_pc_agent.bat`
- **開始（オプション）：**
  `C:\_TsuyamaSignage\app\signagePC`
- **トリガー：** 起動時
- **遅延：** 2分

#### C. Tsuyama Auto Play Start
- **プログラム/スクリプト：**
  `C:\_TsuyamaSignage\app\signagePC\start_auto_play.bat`
- **開始（オプション）：**
  `C:\_TsuyamaSignage\app\signagePC`
- **トリガー：** ログオン時（SystemAV）
- **遅延：** 30秒

**BATファイル名は最終固定**
- `start_auto_play.bat`
- `start_pc_agent.bat`

---

## 10. 再起動して自動再生を確認
1. 再起動
2. 何も触らず待つ
3. 自動で再生が始まれば完了

---

## 困ったときはこれだけ
- 動かない → **C:\_TsuyamaSignage をコピーし直す**
- 触っていいのは → **content フォルダだけ**
- それ以外は → **触らない**
