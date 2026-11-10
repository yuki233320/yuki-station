#!/usr/bin/env python3
"""Build an isolated, relocatable macOS arm64 app; never copy user data."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
RUNTIME = 'cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz'
RUNTIME_SHA = '353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e'
VERSION = '0.2.8'
BUILD = '12'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(path):
    files = {p.name: digest(p) for p in sorted(path.glob('*.py')) if not p.name.startswith('test_')}
    return hashlib.sha256('\n'.join(k + ':' + v for k, v in files.items()).encode()).hexdigest()



def _package_module(source):
    spec = importlib.util.spec_from_file_location('desktop_package_source', source / 'package_source.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def release_inputs(source):
    """Fingerprint all whitelist source, bridges and native build inputs.

    Runtime identity stays Python-only; release integrity additionally covers
    UI assets and native resources. This does not read data or local settings.
    """
    source = Path(source).resolve(strict=True)
    module = _package_module(source)
    entries = [('README.md', source / 'SOURCE_README.md'), ('LICENSE', source / 'LICENSE')]
    entries.extend(('lancer-local/' + name, source / name) for name in module.SOURCE_FILES)
    entries.extend(('lancer-mcp-bridge/' + name, source.parent / 'lancer-mcp-bridge' / name) for name in module.MCP_FILES)
    entries.extend(('lancer-codex-bridge/' + name, source.parent / 'lancer-codex-bridge' / name) for name in module.BRIDGE_FILES)
    hashes = {}
    for name, path in entries:
        if not name or Path(name).is_absolute() or '..' in Path(name).parts or name in hashes:
            raise ValueError('Invalid or duplicate release source name')
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source.parent):
            raise ValueError('Unsafe or missing release source: ' + name)
        hashes[name] = digest(path)
    packaged = [{'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                for name, raw in module.read_sources(entries)]
    resources = ['AppIcon.icns', 'Info.plist', 'Main.swift', 'README.txt', 'build_release.py',
                 'runtime-source.json', RUNTIME]
    license_root = ROOT / 'runtime-licenses'
    if not license_root.is_dir() or license_root.is_symlink():
        raise ValueError('Runtime license directory is missing or unsafe')
    resources += [p.relative_to(ROOT).as_posix() for p in sorted(license_root.rglob('*')) if p.is_file() or p.is_symlink()]
    desktop = {}
    for name in resources:
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Unsafe or missing desktop build input: ' + name)
        desktop[name] = digest(path)
    return {'sourceFiles': hashes, 'packagedSources': packaged, 'desktopInputs': desktop}


def freeze_candidate(source, basis):
    """Return a complete explicit freeze record; the caller chooses when to save."""
    return {'schema': 'yuki-release-source-freeze/v2', 'fingerprint': snapshot(Path(source)),
            'version': VERSION, 'build': BUILD, 'frozenAt': datetime.now(timezone.utc).isoformat(),
            'basis': basis, **release_inputs(source)}


def validate_freeze(source, frozen):
    if (frozen.get('schema') != 'yuki-release-source-freeze/v2'
            or frozen.get('fingerprint') != snapshot(source)
            or frozen.get('version') != VERSION or frozen.get('build') != BUILD):
        raise ValueError('Source is not the explicitly frozen release candidate')
    actual = release_inputs(source)
    if any(frozen.get(key) != actual[key] for key in actual):
        raise ValueError('Release whitelist, UI, bridge or native resources changed after freeze')


def unpack_runtime(destination):
    archive = ROOT / RUNTIME
    if digest(archive) != RUNTIME_SHA:
        raise ValueError('Runtime digest does not match the pinned official release')
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            name = Path(member.name)
            if name.is_absolute() or '..' in name.parts or name.parts[0] != 'python':
                raise ValueError('Unsafe runtime archive member')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError('Unsupported runtime archive member')
            if member.issym() or member.islnk():
                target = ((destination / name).parent / member.linkname).resolve() if member.issym() else (destination / member.linkname).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError('Runtime archive link escapes extraction root')
        tar.extractall(destination)


def _build(source, output, app, frozen):
    module = _package_module(source)
    archive = app.parent / 'source.zip'
    source_manifest = module.build_archive(archive)
    if source_manifest['files'] != frozen['packagedSources']:
        raise ValueError('Packaged source bytes differ from the frozen whitelist')
    resources = app / 'Contents/Resources'; macos = app / 'Contents/MacOS'
    resources.mkdir(parents=True); macos.mkdir()
    with tempfile.TemporaryDirectory(prefix='yuki-app-build-', dir=output) as folder:
        temporary = Path(folder)
        with zipfile.ZipFile(archive) as packed:
            packed.extractall(temporary)
        # The source review ZIP may include local development history. The desktop
        # app ships production source/assets and licenses, never development notes.
        for item in (temporary / module.PREFIX).rglob('*'):
            relative = item.relative_to(temporary / module.PREFIX)
            if not item.is_file():
                continue
            if len(relative.parts) == 1:
                include = relative.name == 'LICENSE'
            elif relative.parts[0] == 'lancer-local':
                name = relative.parts[-1]
                include = relative.parts[1] == 'public' or len(relative.parts) == 2 and (name.endswith('.py') and not name.startswith('test_') or name == 'LICENSE')
            else:
                include = 'test' not in relative.parts and relative.name not in ('README.md',)
            if include:
                target = resources / 'server' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        unpack_runtime(temporary)
        shutil.move(str(temporary / 'python'), resources / 'runtime')
    licenses = ROOT / 'runtime-licenses/python'
    if not (licenses / 'PYTHON.json').is_file() or not (licenses / 'licenses/LICENSE.cpython.txt').is_file():
        raise ValueError('Runtime license metadata is required')
    metadata = json.loads((licenses / 'PYTHON.json').read_text())
    required = {metadata['license_path']}
    for variants in metadata['build_info']['extensions'].values():
        for variant in variants:
            required.update(variant.get('license_paths', []))
    if any(not (licenses / name).is_file() for name in required):
        raise ValueError('A runtime dependency license is missing')
    shutil.copytree(licenses, resources / 'runtime-licenses')
    shutil.copy2(ROOT / 'runtime-source.json', resources / 'runtime-source.json')
    shutil.copy2(ROOT / 'AppIcon.icns', resources / 'AppIcon.icns')
    shutil.copy2(ROOT / 'README.txt', resources / 'README.txt')
    with (ROOT / 'Info.plist').open('rb') as f: info = plistlib.load(f)
    info.update(CFBundleShortVersionString=VERSION, CFBundleVersion=BUILD, LSMinimumSystemVersion='12.0')
    for key in ('YukiServerScript', 'YukiPythonExecutable'): info.pop(key, None)
    with (app / 'Contents/Info.plist').open('wb') as f: plistlib.dump(info, f)
    fingerprint = snapshot(resources / 'server/lancer-local')
    manifest = {'schema': 'yuki-desktop-distribution/v1', 'platform': 'macOS', 'architecture': 'arm64',
                'runtimeFingerprint': fingerprint, 'pythonVersion': '3.12.16', 'runtimeArchiveSha256': RUNTIME_SHA,
                'sourceArchiveSha256': digest(archive), 'reviewSourceFiles': len(source_manifest['files']),
                'bundledSourceFiles': sum(1 for p in (resources / 'server').rglob('*') if p.is_file()),
                'userDataIncluded': False, 'credentialsIncluded': False,
                'defaultDataDirectory': '~/Library/Application Support/YukiStation/data',
                'signing': 'ad-hoc; no Apple Developer ID/notarization',
                'optionalTools': 'Node/MCP/browser/Codex require separate user setup; no credentials bundled'}
    (resources / 'desktop-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    subprocess.run(['xcrun', 'swiftc', str(ROOT / 'Main.swift'), '-parse-as-library', '-target',
                    'arm64-apple-macosx12.0', '-module-cache-path', str(ROOT / '.swift-module-cache'),
                    '-framework', 'AppKit', '-framework', 'WebKit', '-o', str(macos / 'YukiStation')], check=True)
    subprocess.run(['codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    manifest['appFiles'] = sum(1 for p in app.rglob('*') if p.is_file())
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    package = output / 'YukiStation-macOS-arm64.zip'
    subprocess.run(['ditto', '-c', '-k', '--keepParent', str(app), str(package)], check=True)
    (output / 'SHA256SUMS').write_text(digest(package) + '  ' + package.name + '\n')
    return {'app': str(app), 'package': str(package), **manifest}


def build(source, output, keep_app=False):
    """Keep the distributable ZIP, not extra applications in macOS app lists."""
    source, output = source.resolve(strict=True), output.resolve()
    frozen = json.loads((ROOT / 'source-freeze.json').read_text())
    validate_freeze(source, frozen)
    products = ('Yuki雪站.app', 'YukiStation-macOS-arm64.zip',
                'release-manifest.json', 'SHA256SUMS', 'source.zip')
    if any((output / name).exists() for name in products):
        raise ValueError('Output already contains a build; preserve it and choose another output')
    output.mkdir(parents=True, exist_ok=True)
    if keep_app:
        result = _build(source, output, output / 'Yuki雪站.app', frozen)
        validate_freeze(source, frozen)
        return {**result, 'appRetained': True}
    # A failure also cleans the temporary application. Only explicit installation
    # or debugging may retain a second .app outside the installed app location.
    with tempfile.TemporaryDirectory(prefix='.yuki-desktop-', suffix='.noindex', dir=output) as folder:
        result = _build(source, output, Path(folder) / 'Yuki雪站.app', frozen)
        validate_freeze(source, frozen)
    return {**result, 'app': None, 'appRetained': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT.parent / 'source/lancer-local')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--keep-app', action='store_true',
                        help='Retain an unpacked .app for installation/debugging; default keeps only ZIP and release records')
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, keep_app=args.keep_app), ensure_ascii=False, indent=2))
