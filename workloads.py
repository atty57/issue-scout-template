"""Read-only workload detection and validation plans for the supported ecosystems."""
import json
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


RUNTIME_IMAGES = {
    "python312": "python:3.12", "node22": "node:22", "node24": "node:24", "node26": "node:26",
    "dotnet8": "mcr.microsoft.com/dotnet/sdk:8.0", "dotnet9": "mcr.microsoft.com/dotnet/sdk:9.0",
    "dotnet10": "mcr.microsoft.com/dotnet/sdk:10.0", "c_gcc14": "gcc:14",
    "go126": "golang:1.26", "go127": "golang:1.27", "rust1": "rust:1",
    "java21_maven": "maven:3-eclipse-temurin-21", "java25_maven": "maven:3-eclipse-temurin-25",
    "java21_gradle": "gradle:jdk21", "java25_gradle": "gradle:jdk25", "cpp_gcc14": "gcc:14",
    "documentation": "python:3.12",
}
# Keep scout.json's "languages" in this order and in step (test_languages.py checks it).
LANGUAGE_RUNTIMES = {
    "python": ["python312"], "javascript": ["node22", "node24", "node26"],
    "typescript": ["node22", "node24", "node26"], "csharp": ["dotnet8", "dotnet9", "dotnet10"],
    "c": ["c_gcc14"], "go": ["go126", "go127"], "rust": ["rust1"],
    "java": ["java21_maven", "java25_maven", "java21_gradle", "java25_gradle"], "cpp": ["cpp_gcc14"],
}
MAVEN_FILES = {"pom.xml", "mvnw"}
GRADLE_FILES = {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradlew"}
CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")
NATIVE_BUILD_FILES = {"Makefile", "CMakeLists.txt", "meson.build", "configure.ac"}
DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}
TEST_PHASE = "SCOUT_TEST_PHASE_STARTED"


def canonical_language(value):
    value = value.lower().strip()
    return {"c#": "csharp", "c sharp": "csharp", "js": "javascript", "ts": "typescript",
            "c++": "cpp", "c plus plus": "cpp", "golang": "go"}.get(value, value)


def safe_path(value):
    return (isinstance(value, str) and bool(value) and not value.startswith(("/", "-"))
            and not any(c in value for c in "\\:*?[]\x00\n\r") and ".." not in PurePosixPath(value).parts)


def documentation_path(name):
    path = PurePosixPath(name)
    return (path.suffix.lower() in DOC_SUFFIXES and path.name != "CMakeLists.txt"
            and not re.fullmatch(r"requirements(?:[._-].*)?\.txt", path.name, re.I))


def manifest_rank(path):
    parts = PurePosixPath(path).parts
    return (bool(set(parts[:-1]) & {"docs", "examples", "tests", "test", "fixtures"}), len(parts), path)


def inspect_languages(paths, read_file, python_context, truncated=False):
    """Manifests are evidence, not executable installation recipes."""
    contexts = {"python": python_context}
    paths = [p for p in paths if not set(PurePosixPath(p).parts) & {"node_modules", "vendor", ".venv", "venv"}]
    node_manifests = sorted((p for p in paths if PurePosixPath(p).name == "package.json"), key=manifest_rank)
    node_packages = []
    for filename in node_manifests[:5]:
        try:
            package = json.loads(read_file(filename))
            if not isinstance(package, dict):
                continue
            scripts = package.get("scripts", {})
            if not isinstance(scripts, dict):
                continue
            script_names = sorted(scripts, key=lambda name: (not any(term in name for term in ("test", "build", "docs", "lint")), name))
            node_packages.append({"path": str(PurePosixPath(filename).parent),
                                  "engines": package.get("engines", {}),
                                  "package_manager": package.get("packageManager", ""),
                                  "scripts": {name: str(scripts[name])[:400] for name in script_names[:12]},
                                  "test_tools": [name for name in {**package.get("dependencies", {}), **package.get("devDependencies", {})}
                                                 if name in {"typescript", "vitest", "jest", "mocha", "ava", "tsx", "ts-node", "playwright"}],
                                  "lockfiles": [p for p in paths if str(PurePosixPath(p).parent)
                                                == str(PurePosixPath(filename).parent)
                                                and PurePosixPath(p).name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}]})
        except (json.JSONDecodeError, TypeError):
            continue
    for language, suffixes in [("javascript", (".js", ".jsx", ".mjs", ".cjs")),
                               ("typescript", (".ts", ".tsx"))]:
        sources = [p for p in paths if p.endswith(suffixes)]
        if sources or node_manifests:
            contexts[language] = {"status": "supported" if node_packages else "unknown",
                                  "reason": "Node package metadata found" if node_packages else "Node setup needs confirmation",
                                  "packages": node_packages, "source_examples": sources[:6],
                                  "runtimes": LANGUAGE_RUNTIMES[language]}
    projects = sorted((p for p in paths if p.endswith(".csproj")), key=manifest_rank)
    targets = set()
    project_metadata = []
    for filename in projects[:5]:
        try:
            config = ET.fromstring(read_file(filename))
            frameworks = [e.text or "" for e in config.iter()
                          if e.tag.rsplit("}", 1)[-1] in {"TargetFramework", "TargetFrameworks", "TargetFrameworkVersion"}]
            targets.update(t.strip() for value in frameworks for t in value.split(";") if t.strip())
            project_metadata.append({"path": filename, "target_frameworks": frameworks})
        except ET.ParseError:
            continue
    if projects or any(p.endswith(".cs") for p in paths):
        runtimes = sorted({f"dotnet{m.group(1)}" for target in targets
                           if (m := re.fullmatch(r"net(8|9|10)\.0", target))})
        library_or_indirect = any(t.startswith("netstandard") or "$(" in t for t in targets)
        unsupported = bool(targets) and not runtimes and not library_or_indirect and len(projects) <= 5 and not truncated
        contexts["csharp"] = {"status": "unsupported" if unsupported else "supported" if runtimes else "unknown",
                              "reason": "Linux .NET SDK target found" if runtimes else "Linux .NET SDK compatibility needs confirmation",
                              "projects": project_metadata, "target_frameworks": sorted(targets),
                              "runtimes": runtimes or LANGUAGE_RUNTIMES["csharp"]}
        if "global.json" in paths:
            try:
                contexts["csharp"]["sdk_selection"] = json.loads(read_file("global.json")).get("sdk", {})
                selection = contexts["csharp"]["sdk_selection"]
                if isinstance(selection, dict) and selection.get("version"):
                    # Floating container tags cannot guarantee a pinned SDK patch/feature band.
                    contexts["csharp"]["status"] = "unknown" if not unsupported else "unsupported"
                    contexts["csharp"]["reason"] = "Pinned SDK needs an explicit compatible setup/test plan"
            except (json.JSONDecodeError, AttributeError):
                contexts["csharp"]["status"] = "unknown"
    c_sources = [p for p in paths if p.endswith(".c")]
    if c_sources:
        build_files = [p for p in paths if PurePosixPath(p).name in NATIVE_BUILD_FILES]
        contexts["c"] = {"status": "supported" if build_files else "unknown",
                         "reason": "C sources and build metadata found" if build_files else "C build/test plan needs confirmation",
                         "build_files": build_files[:8], "source_examples": c_sources[:6], "runtimes": ["c_gcc14"]}
    # Build files are evidence of how a project builds and tests; the solver still proposes the commands.
    def named(names):
        return sorted(p for p in paths if PurePosixPath(p).name in names)

    maven, gradle = named(MAVEN_FILES), named(GRADLE_FILES)
    java_runtimes = [r for r in LANGUAGE_RUNTIMES["java"] if (maven and r.endswith("_maven")) or (gradle and r.endswith("_gradle"))]
    for language, suffixes, build_files, runtimes, kind in [
            ("go", (".go",), named({"go.mod"}), LANGUAGE_RUNTIMES["go"], "Go module"),
            ("rust", (".rs",), named({"Cargo.toml"}), LANGUAGE_RUNTIMES["rust"], "Cargo package"),
            ("java", (".java",), sorted(maven + gradle), java_runtimes or LANGUAGE_RUNTIMES["java"], "Maven or Gradle build"),
            ("cpp", CPP_SUFFIXES, named(NATIVE_BUILD_FILES), LANGUAGE_RUNTIMES["cpp"], "C++ build metadata")]:
        sources = [p for p in paths if p.endswith(suffixes)]
        if sources:
            contexts[language] = {"status": "supported" if build_files else "unknown",
                                  "reason": f"{kind} and sources found" if build_files else f"{kind} needs confirmation",
                                  "build_files": build_files[:8], "source_examples": sources[:6], "runtimes": runtimes}
    docs = [p for p in paths if documentation_path(p)]
    if docs:
        contexts["documentation"] = {"status": "supported", "reason": "Documentation files found; static checks available",
                                     "files": docs[:12], "runtimes": ["documentation"]}
    return contexts


def validation_script(plan, cwd):
    """Quote argv commands for execution only inside a secretless container."""
    if not isinstance(plan, dict) or plan.get("runtime") not in RUNTIME_IMAGES or not safe_path(cwd):
        raise ValueError("invalid validation runtime or working directory")
    setup, checks = plan["setup_commands"], plan["test_commands"]
    if not isinstance(setup, list) or not isinstance(checks, list) or not checks or len(setup) + len(checks) > 20:
        raise ValueError("a bounded validation plan needs at least one check")
    for command in setup + checks:
        if (not isinstance(command, list) or not command or len(command) > 100
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)):
            raise ValueError("validation commands must be nonempty argv arrays")
    parts = ["set -eu", 'W=$(mktemp -d)', 'cp -r /src/. "$W"', f'cd "$W"/{shlex.quote(cwd)}']
    parts += [shlex.join(command) for command in setup]
    parts += [f"echo {TEST_PHASE}"] + [shlex.join(command) for command in checks]
    return " && ".join(parts)


def check_documentation(repo_dir, files):
    """Static checks of changed text files; no repository commands run on the host."""
    root = Path(repo_dir).resolve()
    if not files:
        return False, "documentation patch is empty"
    errors = []
    for name in files:
        if not safe_path(name) or not documentation_path(name):
            errors.append(f"not a documentation text file: {name}")
            continue
        path = root / name
        if not path.resolve().is_relative_to(root):
            errors.append(f"path escapes checkout: {name}")
            continue
        if not path.exists():  # A deleted documentation file can be an intentional fix.
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError) as e:
            errors.append(f"cannot read {name}: {e}")
            continue
        if re.search(r"(?m)^(?:<<<<<<< |>>>>>>> )", text):
            errors.append(f"merge conflict marker in {name}")
        if path.suffix.lower() not in {".md", ".mdx"}:
            continue
        prose = re.sub(r"```.*?```|~~~.*?~~~|`[^`]*`", "", text, flags=re.S)
        for match in re.finditer(r"!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)", prose):
            link = urlsplit(match.group(1))
            if link.scheme or link.netloc or not link.path:
                continue
            target = unquote(link.path)
            # Extensionless routes and generated HTML need the project's docs build.
            if not PurePosixPath(target).suffix or target.endswith(".html"):
                continue
            dest = root / target.lstrip("/") if target.startswith("/") else path.parent / target
            if not dest.resolve().is_relative_to(root) or not dest.exists():
                errors.append(f"broken local file link in {name}: {target}")
    if errors:
        return False, "\n".join(errors)
    return True, (f"Static documentation checks passed for {len(files)} changed files: UTF-8, conflict markers, "
                  "and explicit local file links. Human review must confirm accuracy; remote links, anchors, "
                  "generated routes and rendered layout are not verified by these checks.")
