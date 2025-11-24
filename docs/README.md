# ImageToJpgApp

ImageToJpgApp は Windows 向けのシンプルな GUI アプリケーションで、複数画像を一括で JPEG に変換します。ドラッグ＆ドロップで直感的に操作でき、主要画像形式（PNG / JPEG / WebP / GIF など）に対応しています。

## インストール（エンドユーザー向け）
1. Releases から最新の `ImageToJpgApp.exe` をダウンロード。  
2. ダウンロードした実行ファイルをダブルクリックして起動。  
3. 必要に応じて右クリック → プロパティ → 「ブロックの解除」を実行してから起動してください（Windows SmartScreen による警告を回避するため）。

## 使い方（基本）
- ファイルまたはフォルダをアプリウィンドウへドラッグ＆ドロップします。  
- 出力先フォルダを指定し、必要なら画質やファイル名ルールを設定します。  
- 「変換」ボタンを押すと、進行ログが表示されながら JPEG に変換されます。  
- 変換完了後、出力フォルダを開いて結果を確認します。

## 開発者向けセットアップ
前提: Windows、Python 3.12（推奨）、仮想環境使用を推奨。

1. リポジトリをクローン:
```powershell
git clone https://github.com/<あなたのユーザ>/ImageToJpgApp.git
cd ImageToJpgApp
```
2. 仮想環境作成と有効化:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
3. 依存関係インストール:
```powershell
pip install -r requirements.txt
```
4. ローカル実行（開発用）:
```powershell
python -m app.main
```

## ビルド（配布用 exe）
1. アイコンを assets/app.ico として配置（任意）。  
2. クリーン:
```powershell
Remove-Item -Recurse -Force .\dist, .\build, .\ImageToJpgApp.spec -ErrorAction SilentlyContinue
```
3. onedir（確認用）ビルド:
```powershell
pyinstaller --noconfirm --clean --onedir --name ImageToJpgApp --icon=assets\app.ico --paths .\.venv\Lib\site-packages app\main.py
```
4. onefile（最終）ビルド:
```powershell
pyinstaller --noconfirm --clean --onefile --windowed --name ImageToJpgApp --icon=assets\app.ico --paths .\.venv\Lib\site-packages app\main.py
```
5. 出力ファイルは `dist\ImageToJpgApp.exe`。配布前に別マシンで動作確認してください。

## Release と配布の推奨ワークフロー
- バイナリは Git の履歴に直接含めず、GitHub Releases に添付して配布してください。  
- リリース前にタグを作成:
```powershell
git tag -a vX.Y.Z -m "vX.Y.Z: release notes"
git push origin vX.Y.Z
```
- リリースに SHA256 チェックサムを添えると受け取り側の信頼性が上がります:
```powershell
Get-FileHash .\dist\ImageToJpgApp.exe -Algorithm SHA256
```

## トラブルシューティング（短く）
- 起動で「No module named 'PyQt5'」が出る: 仮想環境でビルドしているか、`--paths .\.venv\Lib\site-packages` と `--collect-all PyQt5` や適切な `--add-data` を指定して再ビルドしてください。  
- アイコンが反映されない: Explorer のキャッシュの可能性があるためエクスプローラー再起動やサインアウト/再起動を試してください。  
- ダウンロード後に実行できない: プロパティから「ブロックの解除」を試し、それでも警告が出る場合は署名を検討してください。

## 貢献ガイドライン
- Issue に不具合や改善提案を報告してください。再現手順、OS バージョン、ログを添えると対応が早くなります。  
- 機能追加や修正は feature ブランチを切り、プルリクを作成してください。CI が通ることを確認の上でレビュー依頼してください。

## ライセンス
このプロジェクトは適切なライセンス（例: MIT）を明記してください。まだ決めていない場合は LICENSE ファイルを追加することをおすすめします。

---

必要なら README を短縮版にしたり、英語版を用意したり、CI で自動的にリリースを作るワークフロー雛形を生成します。どれを作りますか。