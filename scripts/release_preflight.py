"""Read-only candidate/schema assessment. No migration, execution or switch."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.pg_recovery import DatabaseEndpoint  # noqa: E402
from app.services.recovery_bundle import RecoveryToolError  # noqa: E402
from app.services.release_bundle import ReleaseError  # noqa: E402
from app.services.release_preflight import assess_release,assess_runtime_file  # noqa: E402


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--database-env',default='VIDEO_AI_DATABASE_URL');parser.add_argument('--runtime-env-file')
    args=parser.parse_args(argv)
    try:
        result=assess_release(args.input,args.expected_sha256,DatabaseEndpoint.from_environment(args.database_env))
        if args.runtime_env_file:result['runtime_config']=assess_runtime_file(args.runtime_env_file)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ReleaseError,RecoveryToolError) as error:code=error.code
    except Exception:code='release_preflight_failed'
    print(json.dumps({'ok':False,'code':code,'ready_for_production':False,'migrated':False,'deployed':False}));return 1


if __name__=='__main__':raise SystemExit(main())
