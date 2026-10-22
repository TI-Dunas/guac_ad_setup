#!/usr/bin/env python3
"""
Integra o Guacamole (Docker) ao Active Directory e ajusta tudo para o acesso
direto: o tecnico faz login no Guacamole com a conta de rede e o PC abre sem
pedir senha de novo.

O que o script faz (comando "apply"):
  1. Verifica DNS e a porta LDAP do controlador de dominio.
  2. Cria um docker-compose.override.yml com as variaveis LDAP (o seu compose
     original NAO e alterado) e recria somente o container do Guacamole web.
  3. Confirma que a extensao LDAP carregou.
  4. Cria no Guacamole o grupo com o mesmo nome do grupo do AD e da a ele acesso
     a pasta GLPI e as conexoes dentro dela.
  5. Ajusta as conexoes da pasta GLPI para repassar usuario/senha/dominio.
  6. Ajusta o guac_glpi_bridge.py instalado (PASS_CREDENTIALS, RDP_DOMAIN,
     USER_GROUP) e reinicia o servico guac-glpi.
  7. Opcional: testa o login com uma conta do AD.

Uso (no servidor do Guacamole, como root):
    sudo python3 guac_ad_setup.py check      -> so verifica, nao altera nada
    sudo python3 guac_ad_setup.py apply      -> aplica a configuracao
    sudo python3 guac_ad_setup.py rollback   -> remove o LDAP e volta o bridge
"""

# =========================== CONFIGURE AQUI ===========================

# --- Active Directory (copie de um dos diretorios LDAP do GLPI) ---
LDAP_HOST = "grupovia.lan"          # nome do dominio (usa todos os DCs) ou IP de um DC
LDAP_PORT = 389
LDAP_BASE_DN = "DC=grupovia,DC=lan" # raiz do dominio: alcanca todas as OUs
LDAP_BIND_DN = "CN=svc-guacamole,OU=Services Account,DC=grupovia,DC=lan"
LDAP_BIND_PASSWORD = "TROQUE_AQUI"   # <<< UNICO CAMPO A PREENCHER: senha do svc-guacamole

# Grupo do AD que pode entrar no Guacamole (grupo interno do contêiner Builtin)
AD_GROUP_NAME = "Server Operators"
AD_GROUP_DN = "CN=Server Operators,CN=Builtin,DC=grupovia,DC=lan"

# Dominio NetBIOS enviado ao Windows nas conexoes RDP
RDP_DOMAIN = "GRUPOVIA"

# --- Guacamole ---
GUAC_CONTAINER = "guacamole"        # nome do container web (docker ps)
GUAC_URL = "http://127.0.0.1:8095"
CONN_GROUP = "GLPI"                 # pasta das conexoes criadas pelo bridge
UPDATE_ALL_CONNECTIONS = False      # True = ajusta TODAS as conexoes RDP, nao so a pasta GLPI

# Usuario administrador do Guacamole para a API.
# Vazio = usa o GUAC_USER/GUAC_PASS do bridge instalado (svc-sync).
GUAC_ADMIN_USER = ""
GUAC_ADMIN_PASS = ""

# --- Bridge GLPI -> Guacamole ---
BRIDGE_FILE = "/opt/guac-glpi/guac_glpi_bridge.py"
BRIDGE_SERVICE = "guac-glpi"

# ======================================================================

import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MARKER = "# gerado por guac_ad_setup.py"
STAMP = time.strftime("%Y%m%d-%H%M%S")


def ok(msg):
    print(f"[ok] {msg}", flush=True)


def info(msg):
    print(f"[..] {msg}", flush=True)


def warn(msg):
    print(f"[!!] {msg}", flush=True)


def fail(msg):
    raise SystemExit(f"[erro] {msg}")


def sh(cmd, check=True, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        fail(f"comando falhou: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


# ----------------------------- verificacoes -----------------------------

def check_config():
    if os.geteuid() != 0:
        fail("execute como root (sudo).")
    if LDAP_BIND_PASSWORD == "TROQUE_AQUI":
        fail("preencha LDAP_BIND_PASSWORD (senha do svc-guacamole) no bloco CONFIGURE AQUI.")
    if not AD_GROUP_DN.upper().startswith("CN="):
        fail("AD_GROUP_DN deve ser o DN completo do grupo (CN=...,DC=...).")


def check_ldap_network():
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(LDAP_HOST, LDAP_PORT)})
    except socket.gaierror:
        fail(f"{LDAP_HOST} nao resolve no DNS deste servidor.")
    ok(f"{LDAP_HOST} resolve para {', '.join(ips)}")
    reachable = []
    for ip in ips:
        try:
            with socket.create_connection((ip, LDAP_PORT), timeout=3):
                reachable.append(ip)
        except OSError:
            warn(f"porta {LDAP_PORT} fechada ou inacessivel em {ip}")
    if not reachable:
        fail(f"nenhum controlador de dominio respondeu na porta {LDAP_PORT}.")
    ok(f"porta LDAP {LDAP_PORT} acessivel em {', '.join(reachable)}")


def container_info():
    r = sh(["docker", "inspect", GUAC_CONTAINER], check=False)
    if r.returncode != 0:
        fail(f"container '{GUAC_CONTAINER}' nao encontrado (confira com: docker ps).")
    data = json.loads(r.stdout)[0]
    labels = data["Config"].get("Labels") or {}
    env = dict(e.split("=", 1) for e in data["Config"].get("Env") or [] if "=" in e)
    workdir = labels.get("com.docker.compose.project.working_dir")
    files = labels.get("com.docker.compose.project.config_files", "")
    service = labels.get("com.docker.compose.service")
    if not (workdir and files and service):
        fail("o container nao foi criado pelo docker compose; ajuste o LDAP manualmente.")
    files = [f if os.path.isabs(f) else os.path.join(workdir, f) for f in files.split(",")]
    return workdir, [f for f in files if "override" not in os.path.basename(f)], service, env


def db_prefix(env):
    for prefix in ("POSTGRESQL_", "POSTGRES_", "MYSQL_", "SQLSERVER_"):
        if any(k.startswith(prefix) and k.endswith("HOSTNAME") for k in env):
            return prefix
    return None


def override_path(base_files):
    base = os.path.basename(base_files[0])
    stem = "compose" if base.startswith("compose") else "docker-compose"
    return os.path.join(os.path.dirname(base_files[0]), f"{stem}.override.yml")


# ------------------------------ API Guacamole ------------------------------

class Guac:
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.token = self.ds = None

    @staticmethod
    def _http(method, url, form=None, body=None, token=None):
        headers, data = {}, None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if token:
            headers["Guacamole-Token"] = token
            url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def login(self):
        auth = self._http("POST", f"{GUAC_URL}/api/tokens",
                          form={"username": self.user, "password": self.password})
        self.token = auth["authToken"]
        # usa o banco de dados (onde ficam conexoes e grupos), nao o LDAP
        sources = auth.get("availableDataSources") or [auth["dataSource"]]
        dbs = [s for s in sources if s in ("postgresql", "mysql", "sqlserver")]
        self.ds = dbs[0] if dbs else auth["dataSource"]
        return auth

    def call(self, method, path, body=None):
        url = f"{GUAC_URL}/api/session/data/{self.ds}{path}"
        return self._http(method, url, body=body, token=self.token)

    def logout(self):
        try:
            self._http("DELETE", f"{GUAC_URL}/api/tokens/{self.token}", token=self.token)
        except Exception:  # noqa: BLE001
            pass


def admin_credentials():
    if GUAC_ADMIN_USER and GUAC_ADMIN_PASS:
        return GUAC_ADMIN_USER, GUAC_ADMIN_PASS
    if os.path.exists(BRIDGE_FILE):
        text = open(BRIDGE_FILE).read()
        u = re.search(r'^GUAC_USER\s*=\s*"([^"]*)"', text, re.M)
        p = re.search(r'^GUAC_PASS\s*=\s*"([^"]*)"', text, re.M)
        if u and p:
            return u.group(1), p.group(1)
    fail("preencha GUAC_ADMIN_USER e GUAC_ADMIN_PASS (bridge nao encontrado).")


def wait_guacamole(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"{GUAC_URL}/", timeout=5).read(1)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(3)
    return False


# --------------------------------- passos ---------------------------------

def step_override(base_files, service, env):
    prefix = db_prefix(env)
    path = override_path(base_files)
    if os.path.exists(path) and MARKER not in open(path).read():
        fail(f"ja existe {path} criado por outra pessoa; mescle as variaveis LDAP manualmente.")

    variables = {
        "LDAP_ENABLED": "true",
        "LDAP_HOSTNAME": LDAP_HOST,
        "LDAP_PORT": str(LDAP_PORT),
        "LDAP_ENCRYPTION_METHOD": "ssl" if LDAP_PORT == 636 else "none",
        "LDAP_SEARCH_BIND_DN": LDAP_BIND_DN,
        "LDAP_SEARCH_BIND_PASSWORD": LDAP_BIND_PASSWORD,
        "LDAP_USER_BASE_DN": LDAP_BASE_DN,
        "LDAP_USERNAME_ATTRIBUTE": "sAMAccountName",
        "LDAP_USER_SEARCH_FILTER":
            f"(&(objectClass=user)(memberOf:1.2.840.113556.1.4.1941:={AD_GROUP_DN}))",
        "LDAP_GROUP_BASE_DN": LDAP_BASE_DN,
        "LDAP_GROUP_SEARCH_FILTER": "(objectClass=group)",
        "LDAP_GROUP_NAME_ATTRIBUTE": "cn",
        "LDAP_MEMBER_ATTRIBUTE": "member",
        "LDAP_MEMBER_ATTRIBUTE_TYPE": "dn",
    }
    if prefix:
        variables[f"{prefix}AUTO_CREATE_ACCOUNTS"] = "true"
    else:
        warn("banco de dados nao identificado nas variaveis do container; "
             "AUTO_CREATE_ACCOUNTS nao foi definido.")

    lines = [MARKER, "services:", f"  {service}:", "    environment:"]
    for k, v in variables.items():
        # "$" vira "$$" (o compose interpreta $); json.dumps gera string YAML valida
        lines.append(f"      {k}: {json.dumps(v.replace('$', '$$'))}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)   # contem a senha da conta de leitura do AD
    ok(f"variaveis LDAP gravadas em {path}")
    return path


def compose_up(workdir, base_files, service, override=None):
    cmd = ["docker", "compose"]
    for f in base_files + ([override] if override else []):
        cmd += ["-f", f]
    cmd += ["up", "-d", service]
    info("recriando o container do Guacamole web...")
    sh(cmd, cwd=workdir)
    if not wait_guacamole():
        fail(f"o Guacamole nao respondeu em {GUAC_URL}. Veja: docker logs {GUAC_CONTAINER}")
    ok("Guacamole no ar")


def step_check_ldap_loaded():
    time.sleep(3)
    logs = sh(["docker", "logs", "--tail", "400", GUAC_CONTAINER], check=False)
    text = (logs.stdout + logs.stderr).lower()
    if re.search(r"ldap.*loaded|loaded.*ldap", text):
        ok("extensao LDAP carregada")
    else:
        warn(f"nao encontrei a confirmacao da extensao LDAP no log; "
             f"confira: docker logs {GUAC_CONTAINER} 2>&1 | grep -i ldap")


def step_group_and_connections(guac):
    groups = guac.call("GET", "/connectionGroups") or {}
    gid = next((g["identifier"] for g in groups.values() if g["name"] == CONN_GROUP), None)
    if gid is None:
        warn(f"pasta '{CONN_GROUP}' nao existe no Guacamole; pulando permissoes da pasta.")

    # 1) grupo de usuarios com o mesmo nome do grupo do AD
    enc = urllib.parse.quote(AD_GROUP_NAME, safe="")
    user_groups = guac.call("GET", "/userGroups") or {}
    if AD_GROUP_NAME not in user_groups:
        guac.call("POST", "/userGroups", {"identifier": AD_GROUP_NAME, "attributes": {}})
        ok(f"grupo '{AD_GROUP_NAME}' criado no Guacamole")
    else:
        ok(f"grupo '{AD_GROUP_NAME}' ja existe no Guacamole")

    # 2) conexoes a ajustar
    connections = guac.call("GET", "/connections") or {}
    targets = [c for c in connections.values()
               if c.get("protocol") == "rdp"
               and (UPDATE_ALL_CONNECTIONS or (gid and c.get("parentIdentifier") == gid))]

    # 3) permissoes (so adiciona as que faltam)
    perms = guac.call("GET", f"/userGroups/{enc}/permissions") or {}
    have_conn = perms.get("connectionPermissions") or {}
    have_grp = perms.get("connectionGroupPermissions") or {}
    patch = []
    if gid and "READ" not in have_grp.get(gid, []):
        patch.append({"op": "add", "path": f"/connectionGroupPermissions/{gid}", "value": "READ"})
    for c in targets:
        if "READ" not in have_conn.get(c["identifier"], []):
            patch.append({"op": "add", "path": f"/connectionPermissions/{c['identifier']}",
                          "value": "READ"})
    if patch:
        guac.call("PATCH", f"/userGroups/{enc}/permissions", patch)
    ok(f"permissoes do grupo '{AD_GROUP_NAME}': {len(patch)} adicionada(s)")

    # 4) repasse de credenciais nas conexoes
    changed = 0
    for c in targets:
        cid = c["identifier"]
        conn = guac.call("GET", f"/connections/{cid}")
        params = guac.call("GET", f"/connections/{cid}/parameters") or {}
        wanted = {"username": "${GUAC_USERNAME}", "password": "${GUAC_PASSWORD}",
                  "domain": RDP_DOMAIN}
        if all(params.get(k) == v for k, v in wanted.items()):
            continue
        params.update(wanted)
        guac.call("PUT", f"/connections/{cid}",
                  {"name": conn["name"], "parentIdentifier": conn["parentIdentifier"],
                   "protocol": conn["protocol"], "parameters": params,
                   "attributes": conn.get("attributes") or {}})
        changed += 1
    ok(f"conexoes ajustadas para repassar a credencial: {changed} de {len(targets)}")


def set_var(text, name, value):
    new, n = re.subn(rf"^{name}\s*=.*$", f"{name} = {value}", text, flags=re.M)
    return new if n else text


def step_bridge():
    if not os.path.exists(BRIDGE_FILE):
        warn(f"{BRIDGE_FILE} nao encontrado; ajuste do bridge pulado.")
        return
    backup = f"{BRIDGE_FILE}.bak-{STAMP}"
    shutil.copy2(BRIDGE_FILE, backup)
    text = open(BRIDGE_FILE).read()
    text = set_var(text, "PASS_CREDENTIALS", "True")
    text = set_var(text, "PREFILL_USERNAME", "False")
    text = set_var(text, "RDP_DOMAIN", json.dumps(RDP_DOMAIN))
    text = set_var(text, "USER_GROUP", json.dumps(AD_GROUP_NAME))
    with open(BRIDGE_FILE, "w") as f:
        f.write(text)
    sh(["systemctl", "restart", BRIDGE_SERVICE], check=False)
    ok(f"bridge ajustado (backup em {backup}) e servico {BRIDGE_SERVICE} reiniciado")


def step_test_login():
    print()
    user = input("Testar o login com uma conta do AD? Digite o usuario (Enter para pular): ").strip()
    if not user:
        return
    pwd = getpass.getpass(f"Senha de {user}: ")
    try:
        auth = Guac(user, pwd).login()
    except urllib.error.HTTPError as e:
        warn(f"login de '{user}' recusado (HTTP {e.code}). Veja: "
             f"docker logs --tail 50 {GUAC_CONTAINER} 2>&1 | grep -iE 'ldap|auth'")
        return
    sources = auth.get("availableDataSources", [])
    if "ldap" in sources:
        ok(f"login de '{user}' pelo AD funcionou (fontes: {', '.join(sources)})")
    else:
        warn(f"login funcionou, mas nao pelo LDAP (fontes: {', '.join(sources)}). "
             f"Existe um usuario local com esse nome?")


# -------------------------------- comandos --------------------------------

def cmd_check():
    check_config()
    check_ldap_network()
    workdir, base_files, service, env = container_info()
    ok(f"compose: {', '.join(base_files)} (servico '{service}')")
    ok(f"banco de dados: {db_prefix(env) or 'nao identificado'}")
    info(f"override que sera criado: {override_path(base_files)}")
    user, pwd = admin_credentials()
    guac = Guac(user, pwd)
    try:
        guac.login()
    except Exception as e:  # noqa: BLE001
        fail(f"login na API do Guacamole como '{user}' falhou: {e}")
    ok(f"API do Guacamole acessivel como '{user}' (datasource {guac.ds})")
    guac.logout()
    print("\nTudo pronto. Rode: sudo python3 guac_ad_setup.py apply")


def cmd_apply():
    check_config()
    check_ldap_network()
    workdir, base_files, service, env = container_info()
    override = step_override(base_files, service, env)
    compose_up(workdir, base_files, service, override)
    step_check_ldap_loaded()

    user, pwd = admin_credentials()
    guac = Guac(user, pwd)
    try:
        guac.login()
    except Exception as e:  # noqa: BLE001
        fail(f"login na API do Guacamole como '{user}' falhou: {e}")
    try:
        step_group_and_connections(guac)
    finally:
        guac.logout()

    step_bridge()
    step_test_login()
    print(f"""
Concluido. Agora o tecnico entra no Guacamole com a conta do AD (membro de
'{AD_GROUP_NAME}') e as conexoes abrem direto, sem pedir senha de novo.

Se algo der errado:  sudo python3 guac_ad_setup.py rollback
""")


def cmd_rollback():
    if os.geteuid() != 0:
        fail("execute como root (sudo).")
    workdir, base_files, service, _ = container_info()
    path = override_path(base_files)
    if os.path.exists(path) and MARKER in open(path).read():
        os.rename(path, f"{path}.removido-{STAMP}")
        ok("override do LDAP desativado")
        compose_up(workdir, base_files, service)
    else:
        info("nenhum override criado por este script foi encontrado.")
    backups = sorted(p for p in os.listdir(os.path.dirname(BRIDGE_FILE) or ".")
                     if p.startswith(os.path.basename(BRIDGE_FILE) + ".bak-")) \
        if os.path.isdir(os.path.dirname(BRIDGE_FILE)) else []
    if backups:
        first = os.path.join(os.path.dirname(BRIDGE_FILE), backups[0])
        shutil.copy2(first, BRIDGE_FILE)
        sh(["systemctl", "restart", BRIDGE_SERVICE], check=False)
        ok(f"bridge restaurado a partir de {first}")
    warn("as conexoes e o grupo criados no Guacamole foram mantidos "
         "(remova pela interface se quiser).")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    {"check": cmd_check, "apply": cmd_apply, "rollback": cmd_rollback}.get(
        command, lambda: print(__doc__))()
