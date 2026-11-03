"""Build an image from a checksum-pinned source bundle. Never push or deploy."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.recovery_bundle import private_directory,private_file,write_json,RecoveryToolError  # noqa: E402
from app.services.release_bundle import extract_release,ReleaseError  # noqa: E402


def build_image(archive,expected_sha256,output_directory,*,component='control'):
    if component not in {'control','gateway'}:raise ReleaseError('image_component_invalid')
    output=private_directory(output_directory)
    source=output/'source';extracted=extract_release(archive,expected_sha256=expected_sha256,destination=source)
    content=extracted['content_id'];tag='video-ai-'+component+':review-'+content[:16]
    dockerfile=source/'infra/Dockerfile.control'
    base=re.search(r'^ARG PYTHON_IMAGE=(\S+)$',dockerfile.read_text(encoding='utf-8'),re.M)
    if not base or not re.fullmatch(r'python:[a-zA-Z0-9._-]+@sha256:[a-f0-9]{64}',base[1]):raise ReleaseError('pinned_python_base_required')
    log=output/'build.log';identifier=output/'image-id.txt'
    with private_file(log) as stream:
        result=subprocess.run(['docker','build','--progress','plain','--file',str(dockerfile),
            '--build-arg','SOURCE_CONTENT_ID='+content,'--build-arg','INSTALL_EXTRAS='+('gateway,ice,fc' if component=='gateway' else ''),
            '--iidfile',str(identifier),'--tag',tag,str(source)],stdout=stream,stderr=subprocess.STDOUT,timeout=900,check=False)
    if result.returncode:raise ReleaseError('image_build_failed_see_private_build_log')
    image_id=identifier.read_text(encoding='utf-8').strip()
    if not re.fullmatch('sha256:[a-f0-9]{64}',image_id):raise ReleaseError('image_id_unconfirmed')
    probe=subprocess.run(['docker','image','inspect','--format','{{json .Config.Labels}}',image_id],capture_output=True,text=True,check=False,timeout=30)
    if probe.returncode or json.loads(probe.stdout).get('ink.huayuan.video-ai.source-content-id')!=content:
        raise ReleaseError('image_source_label_unconfirmed')
    receipt={'ok':True,'component':component,'image_id':image_id,'tag':tag,'base_image':base[1],
        'source_content_id':content,'archive_sha256':expected_sha256,'source_label_verified':True,
        'build_log':'build.log','pushed':False,'deployed':False,'runtime_acceptance_run':False,'stage_validation_run':False}
    write_json(output/'receipt.json',receipt);return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--output-directory',required=True);parser.add_argument('--component',choices=['control','gateway'],default='control')
    args=parser.parse_args(argv)
    try:
        result=build_image(args.input,args.expected_sha256,args.output_directory,component=args.component)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (ReleaseError,RecoveryToolError) as error:code=error.code
    except subprocess.TimeoutExpired:code='image_build_observation_timed_out_do_not_automatically_repeat'
    except Exception:code='image_build_operation_failed'
    print(json.dumps({'ok':False,'code':code,'pushed':False,'deployed':False}));return 1


if __name__=='__main__':raise SystemExit(main())
