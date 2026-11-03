"""Build/verify/extract a complete source candidate without deployment."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.release_bundle import build_release,verify_release,extract_release,verify_tree,ReleaseError  # noqa: E402
from app.services.recovery_bundle import RecoveryToolError  # noqa: E402


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    build=commands.add_parser('build');build.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));build.add_argument('--output-directory',required=True);build.add_argument('--label',required=True)
    for name in ('verify','extract'):
        command=commands.add_parser(name);command.add_argument('--input',required=True);command.add_argument('--expected-sha256',required=True)
        if name=='extract':command.add_argument('--destination',required=True)
    tree=commands.add_parser('verify-tree');tree.add_argument('--root',required=True);tree.add_argument('--expected-content-id',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='build':result=build_release(args.root,args.output_directory,label=args.label)
        elif args.command=='verify':result=verify_release(args.input,expected_sha256=args.expected_sha256)
        elif args.command=='extract':result=extract_release(args.input,expected_sha256=args.expected_sha256,destination=args.destination)
        else:result=verify_tree(args.root,expected_content_id=args.expected_content_id)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ReleaseError,RecoveryToolError) as error:code=error.code
    except Exception:code='release_operation_failed'
    print(json.dumps({'ok':False,'code':code,'deployed':False}));return 1


if __name__=='__main__':raise SystemExit(main())
