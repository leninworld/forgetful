"""Real SQLite HTTP sensitivity controls; scratch mutants never enter publication."""
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET


def verify_ownership_mutants(root, evidence, mode, env, run, all_ids, classify):
    selected = [n for n in all_ids if '::test_page_ownership_not_hidden_by_parent_filter[' in n]
    assert len(selected) == len(set(selected)) == 7
    outcomes = []
    for variant in ('count', 'page'):
        scratch = Path('/work') / ('ownership-mutant-' + variant)
        shutil.copytree(root, scratch)
        edits = []
        for resource in ('project', 'plan', 'task', 'document', 'code_artifact', 'file', 'skill'):
            path = scratch / 'app/repositories/sqlite' / (resource + '_repository.py')
            original = path.read_text()
            match = re.search(r'    async def list_\w+_page\(', original)
            assert match
            end = original.find('\n    async def ', match.end())
            end = len(original) if end < 0 else end
            method = original[match.start():end]
            assert method.count('.where(*conditions)') == 2
            index = method.find('.where(*conditions)') if variant == 'count' else method.rfind('.where(*conditions)')
            patched = method[:index] + method[index:].replace('.where(*conditions)', '.where(*conditions[1:])', 1)
            path.write_text(original[:match.start()] + patched + original[end:])
            edits.append(str(path.relative_to(scratch)))
        result = evidence / (mode + '-ownership-' + variant + '.xml')
        selection = evidence / (mode + '-ownership-' + variant + '-selection.json')
        mutant_env = dict(env, PYTHONPATH=str(scratch) + ':/work/rootpkg:/packet', SELECTION_EVIDENCE_FILE=str(selection))
        run('ownership-' + variant,
            [sys.executable, '-m', 'pytest', '-p', 'selection_identity', '-p', 'no:cacheprovider',
             *selected, '-m', 'not e2e', '-q', '--basetemp=/tmp/ownership-' + variant,
             '--junitxml=' + str(result)],
            expected=1, cwd=scratch, env=mutant_env, seconds=120)
        facts = json.loads(selection.read_text())
        cases = list(ET.parse(result).getroot().iter('testcase'))
        row = {'variant': variant, 'selected': facts['selected_nodeids'],
               'reports': facts['reports'], 'xml_cases': len(cases), 'mutated_paths': edits,
               'assertion_failures': sum(c.find('failure') is not None for c in cases),
               'errors': sum(c.find('error') is not None for c in cases)}
        outcomes.append(row)
        (evidence / 'ownership-mutants.json').write_text(json.dumps(outcomes, indent=2))
        assert facts['exitstatus'] == 1 and not facts['collection_reports'] and not facts['deselected_nodeids']
        assert set(facts['selected_nodeids']) == set(selected) and len(facts['selected_nodeids']) == 7
        assert len(cases) == 7 and row['assertion_failures'] == 7 and row['errors'] == 0
        classify(facts, selected, set(selected))
        assert all(c.find('skipped') is None for c in cases)
    return outcomes
