# NativeLab

> [!CAUTION]
> **NativeLab é uma prova de conceito experimental, não auditada e
> potencialmente perigosa. Não a trate como uma fronteira de segurança para
> malware, instaladores desconhecidos, dependências hostis ou dados valiosos.**

NativeLab mantém uma sessão `bubblewrap` persistente por workspace. Chamadas
separadas entram nessa sessão por OpenSSH sobre Unix socket e compartilham os
mesmos namespaces, inclusive o localhost privado:

```text
native-lab run npm run dev
                    │
                    │ mesma sessão / 127.0.0.1
                    ▼
native-lab run npx @playwright/mcp ...
```

O SSH transporta stdin, stdout, stderr, EOF e exit status. Não há protocolo
próprio de daemon, multiplexador de pipes ou tmux.

## O alerta de segurança, sem eufemismos

Este PoC reduz bastante o que um processo enxerga, mas ainda compartilha o
kernel e vários recursos escolhidos do host. Um bug no kernel, no bubblewrap,
no parser, nos scripts ou na composição dos mounts pode romper as premissas.

Em particular:

- o workspace atual é persistente e read-write; código executado pode alterar
  ou apagar quase qualquer arquivo nele, exceto os metadados mascarados;
- `.env`, `*.pem`, `*.key` e outros secrets do workspace atual são acessíveis
  deliberadamente;
- `~/.npm-global` e `~/.npm`, quando existem, são montados read-only. Isso
  impede persistência pela sandbox, mas permite ler e executar conteúdo que já
  existe nesses diretórios;
- `extra_read_only` pode expor qualquer dado que o usuário colocar ali;
- `/etc` é uma visão read-only do host e pode conter informação legível pelo
  usuário;
- projetos trusted são filtrados por nomes/globs, não por classificação do
  conteúdo. Um segredo com nome inesperado continua visível;
- read-only impede escrita pela sandbox, mas não cria um snapshot imutável. Um
  processo no host ainda pode mudar uma origem montada durante a sessão;
- a ausência de Internet reduz caminhos de exfiltração, mas dados ainda podem
  ser escritos no workspace, enviados a outro processo no localhost da sessão
  ou impressos;
- o control plane compartilhado é gravável pela própria sandbox. Código dentro
  dela pode derrubar o socket, destruir chaves efêmeras ou causar denial of
  service;
- não existem cgroups, quota, limites de CPU/memória/processos, seccomp próprio,
  Landlock próprio, pidfd ou proteção contra fork bomb;
- não há isolamento de VM: todos os processos usam o kernel do host;
- a implementação é shell + Python e não recebeu auditoria de segurança.

Use somente em workspaces descartáveis ou versionados, mantenha backups e
execute apenas código cujo risco você aceita. Falha ao criar qualquer
propriedade essencial encerra o startup; não existe fallback para execução
direta no host.

## Filesystem policy v2

A raiz não é mais `--ro-bind / /`. Ela começa vazia com `--tmpfs /`, e somente
origens explicitamente permitidas são montadas:

| Visão dentro da sessão | Policy |
| --- | --- |
| `/bin`, `/sbin`, `/usr`, `/etc`, `/lib`, `/lib64` | host RO, se existirem |
| `/nix/store`, `/run/current-system/sw` | host RO, se existirem |
| `~/.npm-global`, `~/.npm` | host RO, se existirem |
| workspace canônico atual | host RW |
| `.git`, `.agents`, `.codex` do workspace | DENY |
| projetos trusted importados | host RO com masks adicionais |
| HOME real restante | não montado |
| pathname lógico do HOME | tmpfs privado RW |
| `/tmp`, `/run`, `/dev/shm` | privados RW |
| `/proc` | novo procfs do PID namespace |
| `/dev` | conjunto mínimo criado pelo bubblewrap |
| `/var`, `/home` e demais árvores | ausentes, salvo mount explícito |

O HOME mantém o pathname retornado por `getpwuid(3)`, por exemplo
`/home/fabio`, mas seu conteúdo começa privado e vazio. Os mounts npm são
aplicados por cima desse HOME. Assim, symlinks como
`~/.npm-global/bin/npm -> ../lib/node_modules/...` continuam funcionando sem
expor o restante do HOME.

`XDG_RUNTIME_DIR` dentro da sessão é `/run/user/$UID`, criado privadamente com
modo `0700`. Ele não é o `$XDG_RUNTIME_DIR` do host. Portanto D-Bus, Wayland,
PipeWire/PulseAudio, gpg-agent, ssh-agent e sockets X11 do host não atravessam
a fronteira. `/dev/shm` também é privado para permitir navegador/Xvfb sem
compartilhar a memória POSIX do host.

### Workspace atual

O resultado de `realpath "$PWD"` é o único root persistente RW. Secrets que
pertencem ao trabalho ativo continuam disponíveis:

```text
.env                 acessível
cert.pem              acessível
private.key           acessível
.git                  negado
.agents               negado
.codex                negado
```

Para caminhos protegidos existentes, NativeLab valida tipo e identidade e
aplica um mount opaco. Symlinks nesses pontos fazem o startup falhar fechado.

Quando `.git`, `.agents` ou `.codex` ainda não existe, bubblewrap precisa de um
mountpoint real sob o bind RW. A solução escolhida para este PoC é visível no
host: NativeLab cria um **diretório vazio temporário** com esse nome e o mantém
durante toda a sessão. Dentro da sandbox ele fica coberto por tmpfs mode `000`
e read-only, portanto não pode ser lido nem receber conteúdo persistente.

Ownership desses placeholders é registrado sob
`$XDG_RUNTIME_DIR/native-lab/synthetic-mounts/`. `stop` remove apenas um
placeholder ainda vazio, com a identidade esperada e sem outro holder ativo.
Múltiplas sessões são contabilizadas. Após `SIGKILL`, a próxima resolução de
policy descarta markers de PIDs mortos e recupera o estado. Se o path mudou de
tipo ou ganhou conteúdo, NativeLab falha fechado e não o apaga.

Consequência operacional: enquanto uma sessão estiver viva, ferramentas no
host verão esses diretórios vazios no workspace. Isso é uma limitação aceita e
documentada do PoC, não um detalhe invisível da implementação.

### Projetos trusted do Codex

Por padrão, NativeLab lê o config global configurado e reconhece o formato:

```toml
[projects."/mnt/projects/Projects/projeto-a"]
trust_level = "trusted"

[projects."/mnt/projects/Projects/projeto-b"]
trust_level = "untrusted"
```

Somente tabelas sob `projects` cujo `trust_level` é exatamente `"trusted"` são
importadas. Os paths são expandidos, canonicalizados, deduplicados e precisam
existir. O workspace atual mantém precedência RW. `/`, o HOME e qualquer
ancestor que exponha todo o HOME são ignorados com warning.

Cada projeto importado é RO. Os seguintes paths são negados:

```text
.git
.agents
.codex
**/.env
**/.env.*
**/*.pem
**/*.key
**/*.p12
**/*.pfx
```

Os matches são expandidos no host antes do `bwrap` com `rg --files --hidden
--no-ignore`. Um match que seja symlink ou outro objeto inesperado aborta a
sessão. Não existe fallback silencioso que amplie acesso.

## Configuração confiável

O único config NativeLab procurado é:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/native-lab/config.toml
```

Não se lê configuração do workspace, não há `--config` e não existe
`NATIVE_LAB_CONFIG`. O arquivo, quando presente, deve ser regular, pertencer ao
usuário, não ser symlink e não ter escrita para group/others.

Defaults equivalentes:

```toml
version = 1

[codex]
import_trusted_projects = true
config_path = "~/.codex/config.toml"

[filesystem]
extra_read_only = []
extra_trusted_project_deny_globs = []
```

`extra_read_only` adiciona paths aos defaults; não os substitui. Não existe
`extra_read_write`: o workspace permanece o único root persistente RW.
`extra_trusted_project_deny_globs` adiciona globs relativos a cada projeto
trusted. `~` e `~/...` são expandidos sem `eval` usando o HOME real.

Exemplo:

```toml
version = 1

[codex]
import_trusted_projects = true
config_path = "~/.codex/config.toml"

[filesystem]
extra_read_only = ["~/sdk-reference"]
extra_trusted_project_deny_globs = ["**/*.secret", "**/credentials.json"]
```

Tanto esse arquivo quanto o config do Codex são parseados por `python3` com
`tomllib`. O helper produz JSON canônico e argumentos NUL-delimited; ele nunca
gera shell para `eval`.

## Policy digest e identidade da sessão

`POLICY_VERSION=2`. Antes de escolher uma sessão, o launcher resolve e ordena:

- roots RW e RO;
- projetos trusted;
- masks concretos e regras DENY;
- metadata protegida;
- opções relevantes dos configs.

O JSON canônico recebe SHA-256, formando `POLICY_DIGEST`. O session id usa:

```text
workspace + NUL + profile + NUL + policy_version + NUL + policy_digest
```

Assim, adicionar/remover trust no Codex ou mudar `extra_read_only` produz outro
ID e não reutiliza uma sandbox criada com autorização anterior. O manifest
resolvido e o resumo ficam no runtime; `status` mostra digest, número de roots
RO e projetos trusted.

Uma sessão antiga já em execução não perde retroativamente seus mounts quando
o config muda. Pare a sessão antes de revogar acesso se houver processos
long-lived; mudar o config garante não-reuso nas próximas chamadas, não revoga
um processo que já existe.

## Ambiente e host-side TCB

O `bwrap` usa `--clearenv` e define apenas um ambiente básico. Não são
encaminhados automaticamente `SSH_AUTH_SOCK`, D-Bus, display, Xauthority,
gpg-agent, tokens ou credenciais cloud. O comando remoto recebe o PATH original
do caller, HOME privado, TMPDIR privado e XDG_RUNTIME_DIR privado.

O PATH do caller não é usado para construir a sandbox. `bwrap`, `ssh`, `socat`,
`python3`, `rg`, `nohup` e `flock` são resolvidos por um PATH host-side fixo,
canonicalizados e rejeitados se estiverem dentro do workspace. O SSH ignora o
config pessoal do cliente com `-F /dev/null`.

Preservar o PATH remoto permite executar `~/.npm-global/bin/npm` e `npx`, mas
não garante que todo componente desse PATH tenha sido montado. `npm install -g`
no prefixo host falhar por read-only é comportamento esperado. Sem Internet,
`npx -y` só funciona quando o pacote necessário já está disponível.

## Namespaces, rede e capabilities

O holder exige:

```text
--unshare-user --unshare-pid --unshare-ipc
--unshare-net  --unshare-uts --new-session
--cap-drop ALL --die-with-parent
```

Loopback funciona entre processos da mesma sessão. O localhost do host não é
visível, a sessão não é visível pelo localhost do host e não há rota para a
Internet.

Capabilities são atualmente sempre removidas. GDB pode funcionar em alguns
casos sem `CAP_SYS_PTRACE`, dependendo de UID, relação pai/filho, Yama e seccomp.
Programas que realmente precisarem de ptrace ou outra capability exigirão um
profile futuro explícito e um novo fingerprint de policy; este PoC não oferece
essa customização ainda.

## Requisitos e instalação

- Linux com user namespaces e network namespaces habilitados;
- Bash;
- Python 3.11+ (`tomllib` na stdlib);
- bubblewrap, socat e ripgrep;
- cliente e servidor OpenSSH;
- `flock`, `realpath`, `sha256sum`, `stat`, `awk` e ferramentas Unix usuais;
- `$XDG_RUNTIME_DIR` válido.

Mantenha juntos e executáveis:

```text
native-lab
native-labd
native-lab-session
native-lab-policy.py
```

`XDG_RUNTIME_DIR` deve ser absoluto, existir, pertencer ao usuário, ter modo
`0700` e ser gravável/pesquisável. Ausência ou permissão diferente causa erro
explícito.

## Uso

No diretório exato que deve ser RW:

```bash
native-lab run COMMAND [ARGS...]
native-lab status
native-lab stop
```

Exemplos:

```bash
native-lab run npm run dev
native-lab run curl http://127.0.0.1:5173
native-lab run -- sh -c 'exit 37'
echo "$?"
# 37
```

O modo padrão usa `ssh -T`: não há PTY, stdin atravessa, stdout e stderr ficam
separados e o exit status é preservado. Argumentos passam por quoting POSIX
cuidadoso. Um futuro `native-lab-exec` poderia substituir essa camada por argv
serializado e `execve(2)`.

TTY, aplicações full-screen e prompts de senha estão fora desta etapa.

## Runtime e lifecycle

```text
$XDG_RUNTIME_DIR/native-lab/
├── policy-mount.lock
├── synthetic-mounts/
└── <session-id>/
    ├── start.lock
    ├── holder.pid
    ├── session.info
    ├── resolved-policy.json
    ├── deny-mask
    ├── daemon.log
    └── control/
        ├── lab-ssh.sock
        ├── client_ed25519
        ├── ssh_host_ed25519_key
        ├── authorized_keys
        ├── known_hosts
        ├── sshd_config
        └── ...
```

Startup concorrente é serializado por `flock`. Um PID ou socket isolado não
declara prontidão: o cliente executa um probe SSH `true`. O holder supervisiona
o bubblewrap, e `--die-with-parent` encerra a sandbox após morte abrupta.

Antes de sinalizar um holder, `stop` valida PID, start-time de `/proc`, argv,
workspace, session dir e policy digest. Em seguida envia `SIGTERM`, aguarda e
usa `SIGKILL` apenas se necessário. Estado stale é recuperado na próxima
execução.

O control plane continua intencionalmente simples. Cliente e sandbox ainda
enxergam as chaves efêmeras no diretório compartilhado; separar material
host-private, shared e sandbox-private é hardening futuro.

## Testes

Os testes precisam rodar num host que permita user/network namespaces; um
sandbox externo pode retornar `EPERM` antes do NativeLab começar.

```bash
./native-lab-smoke.sh
./native-lab-policy-smoke.sh
```

O primeiro cobre lifecycle, concorrência, namespaces, localhost, ausência de
Internet, streaming, stdio, exit status, quoting e recuperação stale. O segundo
cria configs Codex/NativeLab e projetos temporários para cobrir RW/RO, secrets,
metadata, HOME/runtime/shm privados, npm RO, trusted/untrusted, digest e TCB.

O smoke de policy cria dois arquivos-probe com nome único em `~/.npm-global` e
`~/.npm` para comprovar os mounts default e os remove no cleanup. Se os
diretórios não existirem, ele os cria e depois tenta removê-los somente se
continuarem vazios.

O workflow GitHub Actions roda ambos em `ubuntu-24.04`, instala as dependências,
habilita user namespaces na VM efêmera, valida Bash/Python/ShellCheck, modos
executáveis e ausência de runtime/chaves privadas versionados. O relaxamento de
AppArmor no runner descartável não é recomendação para uma máquina real.

## O que entra no Git

Devem ser versionados os quatro executáveis/helper, smoke tests, documentação,
`.gitignore` e `.github/workflows/ci.yml`. Não devem entrar sockets, logs,
chaves SSH, `resolved-policy.json`, runtimes, fixtures temporárias ou
`__pycache__`. O `.gitignore` cobre os artefatos locais conhecidos e o CI
rejeita runtime antigo e private keys rastreadas.

## Diagnóstico

```bash
native-lab status
```

Falhas de startup exibem as primeiras linhas de `daemon.log`. Erros comuns:

- `XDG_RUNTIME_DIR is not set`: runtime do usuário indisponível;
- `missing dependency`: falta uma ferramenta host-side;
- `Operation not permitted`: kernel, LSM ou sandbox externo bloqueou namespaces;
- `failed to resolve filesystem policy`: config inválido, path inseguro, erro
  no scan ou mask que não pode ser construído fail-closed;
- `Read-only file system`/`Permission denied`: escrita fora dos roots permitidos;
- socket longo demais: use um runtime com pathname menor.

Os probes e comandos que fundamentaram as decisões estão em
[DISCOVERIES.md](DISCOVERIES.md).

## Fora de escopo

Não há protocolo daemon próprio, tmux, C/C++, cgroups, proxy de Internet, proxy
MCP, Xvfb integrado, acesso ao display do host, Docker, pidfd, seccomp próprio
ou profiles de capabilities. Antes de qualquer uso como ferramenta de
segurança são necessários threat model formal, revisão especializada, testes
adversariais e hardening adicional.
