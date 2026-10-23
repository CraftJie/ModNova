# 发布清单（Release Checklist）

> 每次对外发布走一遍。版本号唯一来源是 `modbus_tool/__init__.py` 的 `__version__`，
> exe 的版本资源自动取自它 —— 别在任何地方手写第二份。

## 1. 发布前

- [ ] `python -m unittest discover -s tests` 全绿（逻辑）
- [ ] `python tests/ui_full_test.py` 全绿（界面，需带 Tk 的解释器）
- [ ] `python tests/rtu_full_test.py` 全绿（RTU，需 pyserial）
- [ ] `ruff check modbus_tool && mypy modbus_tool` 无新增告警
- [ ] README / README_EN 的能力表与实际界面一致（没有「有实现、没入口」的项）
- [ ] `__init__.py` 版本号升级并提交

## 2. 打包

```bash
python -m venv build/.venv
build\.venv\Scripts\pip install -r build/requirements-build.txt
build\.venv\Scripts\python build/build.py
```

产物：`build/dist/ModNova/`（目录版）与 `build/dist/ModNova-<版本>-win-x64.zip`（便携包）。

- [ ] 冷启动一次目录版 exe，确认界面正常、版本资源正确
- [ ] 便携 zip 解压到另一个盘符再跑一次（路径无关性）

## 3. 签名

- 自签证书只用于本机验证；**对外发布用正式签名身份**
  （开源项目可向 [SignPath Foundation](https://signpath.org/) 免费申请代码签名证书）。
- 签发完成后删除 `build/certs/password.txt`，口令只留密码库（见 README 安全提醒）。

```powershell
powershell -ExecutionPolicy Bypass -File build/sign.ps1 -Action sign `
    -Password <口令> -Files build/dist/ModNova/ModNova.exe
```

## 4. 发布

- [ ] git tag `v<版本>` 并推送
- [ ] GitHub Release：附便携 zip、更新说明（用户可感知的变化，不写 commit 罗列）
- [ ] 确认 CI（`.github/workflows/ci.yml`）在 tag 提交上全绿
- [ ] README 顶部如需展示最新版本号，与 Release 对齐
