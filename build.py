"""Validate syntax and create a dependency-free local distribution."""
import ast, pathlib, shutil
root=pathlib.Path(__file__).resolve().parent
for name in ('server.py','supervisor.py','lifecycle.py','deployment.py'):
    ast.parse((root/name).read_text())
    (root/'dist').mkdir(exist_ok=True)
    shutil.copy2(root/name,root/'dist'/name)
shutil.copytree(root/'web',root/'dist'/'web',dirs_exist_ok=True)
print('Built dist/: python3 dist/server.py')
