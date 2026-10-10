from pathlib import Path
import json,hashlib,subprocess
initial=json.loads(Path('qa/backend-initial-state.json').read_text())
print('Initial evidence keys:', list(initial))
current={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('backend').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
old=initial['backend_file_sha256']
result={'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
'git_status':subprocess.check_output(['git','status','--short'],text=True),
'backend_hashes':current,
'backend_files_changed_during_audit':[p for p in old if p in current and old[p]!=current[p]],
'mission_added_backend_files':[p for p in current if p not in old]}
result['codex_modified_backend_files'] = [p for p in result['backend_files_changed_during_audit'] if p != 'backend/app/static/index.html']
result['concurrent_dashboard_change'] = 'backend/app/static/index.html' in result['backend_files_changed_during_audit']
Path('qa/backend-final-state.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ('backend_hashes','git_status')},indent=2))
