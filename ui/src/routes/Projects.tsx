/**
 * Projetos (`/projetos`): cadastrar e escolher os sistemas mapeados.
 *
 * A pasta é validada pelo marcador do perfil escolhido, e o perfil é só sugerido pelos
 * marcadores (a CLI não adivinha, a interface também não decide: o usuário confirma).
 * O banco do projeto entra pelo NOME da variável do `.env`; a conexão nunca passa pela interface.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, CircleX, FolderSearch, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { Link } from "react-router";
import { Badge, Button, Card, CardHeader, LoadingRows, Notice } from "../components/ui";
import { request } from "../lib/api";
import { useIsAdmin } from "../lib/auth";
import {
  AddProjectDocument,
  DSN_ENV_EXAMPLES,
  isEnvName,
  profileHint,
  ProfilesDocument,
  type Project,
  projectLabel,
  RemoveProjectDocument,
  SetActiveProjectDocument,
  SuggestProfileDocument,
  UpdateProjectDocument,
  useProjects,
} from "../lib/projects";
import { formatDateTime } from "../lib/utils";

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 text-sm";

type Draft = {
  name: string;
  source: string;
  profile: string;
  schemaFromDb: boolean;
  dsnEnv: string;
};

const EMPTY: Draft = { name: "", source: "", profile: "", schemaFromDb: false, dsnEnv: "" };

export function ProjectsPage() {
  const admin = useIsAdmin();
  const projects = useProjects();
  const list = projects.data?.projects ?? [];
  const [editing, setEditing] = useState<string | null>(null);
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Projetos</h1>
        <p className="text-sm text-muted-foreground">
          Cada sistema mapeado (e-cidade, um projeto zend1…) tem a sua pasta, o seu perfil e o seu grafo. A interface mostra um
          projeto por vez; o cruzamento entre eles aparece na tela de Comunicação.
        </p>
      </header>

      {projects.isPending ? (
        <LoadingRows rows={3} label="Carregando os projetos" />
      ) : (
        <Card>
          <CardHeader title="Projetos cadastrados" />
          {list.length === 0 ? (
            <p className="px-5 py-4 text-sm text-muted-foreground">
              {admin ? "Nenhum projeto ainda. Cadastre o primeiro abaixo." : "Nenhum projeto cadastrado ainda."}
            </p>
          ) : (
            <ul className="divide-y" aria-label="Projetos">
              {list.map((project) =>
                editing === project.slug ? (
                  <li key={project.slug} className="px-5 py-4">
                    <ProjectForm project={project} onDone={() => setEditing(null)} />
                  </li>
                ) : (
                  <ProjectItem key={project.slug} project={project} admin={admin} onEdit={() => setEditing(project.slug)} />
                ),
              )}
            </ul>
          )}
        </Card>
      )}

      {admin ? (
        <Card>
          <CardHeader title="Novo projeto" description="Escolha a pasta, confirme o perfil e cadastre." />
          <div className="px-5 py-4">
            <ProjectForm project={null} onDone={() => undefined} />
          </div>
        </Card>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- lista

function ProjectItem({ project, admin, onEdit }: { project: Project; admin: boolean; onEdit: () => void }) {
  const client = useQueryClient();
  const activate = useMutation({
    mutationFn: () => request(SetActiveProjectDocument, { slug: project.slug }),
    onSuccess: () => void client.invalidateQueries(),
  });
  const remove = useMutation({
    mutationFn: () => request(RemoveProjectDocument, { slug: project.slug }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["projects"] }),
  });
  const [confirming, setConfirming] = useState(false);
  return (
    <li className="space-y-2 px-5 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{project.name}</span>
        <Badge>{project.profile}</Badge>
        {project.active ? <Badge className="border-primary text-primary">ativo</Badge> : null}
        {project.hasGraph ? null : <Badge className="border-warning text-warning">sem grafo</Badge>}
        {project.scanning ? <Badge className="border-primary text-primary">scan em andamento</Badge> : null}
        <span className="text-xs text-muted-foreground">{project.slug}</span>
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
        <dt>Pasta</dt>
        <dd className="font-mono">
          {project.source}
          {project.sourceExists ? "" : " (não existe neste computador)"}
        </dd>
        <dt>Último scan</dt>
        <dd>{formatDateTime(project.lastScan)}</dd>
        <dt>Schema do banco</dt>
        <dd>
          {project.schemaFromDb ? (
            <>
              lido do banco pela variável <code className="font-mono">{project.dsnEnv ?? "PHP_MAP_CATALOG_DSN"}</code>
              {project.dsnAvailable ? "" : " (a variável não está definida no .env)"}
            </>
          ) : (
            "não lido do banco"
          )}
        </dd>
      </dl>
      {admin ? (
        <div className="flex flex-wrap items-center gap-2">
          {project.active ? null : (
            <Button size="sm" variant="outline" disabled={activate.isPending} onClick={() => activate.mutate()}>
              <Check className="h-4 w-4" aria-hidden="true" /> Usar este projeto
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={onEdit}>
            <Pencil className="h-4 w-4" aria-hidden="true" /> Editar
          </Button>
          <Link
            to="/scan"
            onClick={() => {
              if (!project.active) activate.mutate();
            }}
            className="inline-flex h-8 items-center gap-2 rounded-md border border-input bg-card px-3 text-sm font-medium hover:bg-muted"
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" /> {project.hasGraph ? "Atualizar o mapeamento" : "Rodar o scan"}
          </Link>
          {project.active ? null : confirming ? (
            <>
              <Button size="sm" variant="outline" disabled={remove.isPending} onClick={() => remove.mutate()}>
                <Trash2 className="h-4 w-4" aria-hidden="true" /> Confirmar a remoção
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
                Cancelar
              </Button>
              <span className="text-xs text-muted-foreground">Só sai da lista: os arquivos do grafo ficam em disco.</span>
            </>
          ) : (
            <Button size="sm" variant="ghost" aria-label={`Remover ${project.name}`} onClick={() => setConfirming(true)}>
              <Trash2 className="h-4 w-4" aria-hidden="true" /> Remover
            </Button>
          )}
        </div>
      ) : null}
    </li>
  );
}

// --------------------------------------------------------------------------- formulário

function fromProject(project: Project | null): Draft {
  if (!project) return EMPTY;
  return {
    name: project.name,
    source: project.source,
    profile: project.profile,
    schemaFromDb: project.schemaFromDb,
    dsnEnv: project.dsnEnv ?? "",
  };
}

type Check = { ok: boolean; message: string } | null;

function ProjectForm({ project, onDone }: { project: Project | null; onDone: () => void }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>(fromProject(project));
  const [check, setCheck] = useState<Check>(null);
  const [error, setError] = useState<string | null>(null);
  const ids = { name: useId(), source: useId(), profile: useId(), env: useId() };
  const profiles = useQuery({
    queryKey: ["projectProfiles"],
    queryFn: async () => (await request(ProfilesDocument)).projectProfiles,
    staleTime: Infinity,
    meta: { silent: true },
  });
  const set = (changes: Partial<Draft>) => {
    setDraft((current) => ({ ...current, ...changes }));
    setError(null);
  };
  const marker = profiles.data?.find((p) => p.name === draft.profile)?.marker;

  // Sugere o perfil pelos marcadores da pasta; quem confirma é o usuário.
  const suggest = useMutation({
    mutationFn: (source: string) => request(SuggestProfileDocument, { source }),
    meta: { silent: true },
    onSuccess: ({ suggestProfile: found }) => {
      if (!found.exists) {
        setCheck({ ok: false, message: "A pasta não existe neste computador." });
      } else if (found.profile) {
        setDraft((current) => ({ ...current, profile: found.profile ?? current.profile }));
        const where = found.markers.find((m) => m.profile === found.profile)?.marker;
        setCheck({ ok: true, message: `Parece um projeto ${found.profile} (achei ${where}). Confirme o perfil abaixo.` });
      } else {
        const missing = found.markers.map((m) => m.marker).join(" ou ");
        setCheck({ ok: false, message: `Não reconheci o perfil: a pasta não tem ${missing}. Escolha o perfil.` });
      }
    },
    onError: (e) => setCheck({ ok: false, message: e.message }),
  });

  const save = useMutation({
    mutationFn: () => {
      const input = {
        name: draft.name.trim(),
        profile: draft.profile,
        source: draft.source.trim(),
        schemaFromDb: draft.schemaFromDb,
        dsnEnv: draft.dsnEnv.trim() || null,
      };
      return project
        ? request(UpdateProjectDocument, {
            slug: project.slug,
            input: { ...input, clearDsnEnv: draft.dsnEnv.trim() === "" },
          }).then(() => undefined)
        : request(AddProjectDocument, { input }).then(() => undefined);
    },
    meta: { silent: true }, // o motivo aparece no formulário (ex.: qual marcador falta)
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["projects"] });
      void client.invalidateQueries({ queryKey: ["workspace"] });
      void client.invalidateQueries({ queryKey: ["scanDefaults"] });
      setDraft(EMPTY);
      setCheck(null);
      onDone();
    },
    onError: (e) => setError(e.message),
  });

  const envInvalid = draft.dsnEnv.trim() !== "" && !isEnvName(draft.dsnEnv);
  const ready = draft.name.trim() && draft.source.trim() && draft.profile && !envInvalid;

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <form onSubmit={submit} className="space-y-4" aria-label={project ? `Editar ${project.name}` : "Novo projeto"}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor={ids.name} className="text-sm font-medium">
            Nome
          </label>
          <input
            id={ids.name}
            className={INPUT}
            value={draft.name}
            placeholder="Zend App"
            onChange={(event) => set({ name: event.target.value })}
          />
        </div>
        <div className="space-y-1.5">
          <label htmlFor={ids.profile} className="text-sm font-medium">
            Perfil
          </label>
          <select
            id={ids.profile}
            className={INPUT}
            value={draft.profile}
            onChange={(event) => set({ profile: event.target.value })}
          >
            <option value="">Escolha…</option>
            {(profiles.data ?? []).map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
          {draft.profile ? (
            <p className="text-xs text-muted-foreground">{profileHint(draft.profile, marker)}</p>
          ) : null}
        </div>
      </div>

      <div className="space-y-1.5">
        <label htmlFor={ids.source} className="text-sm font-medium">
          Pasta do projeto
        </label>
        <div className="flex gap-2">
          <input
            id={ids.source}
            className={`${INPUT} font-mono`}
            value={draft.source}
            placeholder="/caminho/do/projeto"
            spellCheck={false}
            onChange={(event) => {
              set({ source: event.target.value });
              setCheck(null);
            }}
          />
          <Button
            variant="outline"
            disabled={!draft.source.trim() || suggest.isPending}
            onClick={() => suggest.mutate(draft.source.trim())}
          >
            <FolderSearch className="h-4 w-4" aria-hidden="true" /> Sugerir o perfil
          </Button>
        </div>
        {check ? (
          <p role="status" className={`flex items-center gap-1.5 text-sm ${check.ok ? "text-success" : "text-warning"}`}>
            {check.ok ? (
              <Check className="h-4 w-4" aria-hidden="true" />
            ) : (
              <CircleX className="h-4 w-4" aria-hidden="true" />
            )}
            {check.message}
          </p>
        ) : null}
      </div>

      <fieldset className="space-y-2 rounded-md border px-3 py-3">
        <legend className="px-1 text-sm font-medium">Banco do projeto (opcional)</legend>
        <div className="flex items-start gap-2">
          <input
            id={`${ids.env}-db`}
            type="checkbox"
            className="mt-0.5 h-4 w-4 accent-primary"
            checked={draft.schemaFromDb}
            onChange={(event) => set({ schemaFromDb: event.target.checked })}
          />
          <label htmlFor={`${ids.env}-db`} className="text-sm">
            Ler o schema do banco no scan (somente leitura)
          </label>
        </div>
        <div className="space-y-1.5">
          <label htmlFor={ids.env} className="text-sm font-medium">
            Variável do .env com a conexão
          </label>
          <input
            id={ids.env}
            className={`${INPUT} font-mono`}
            value={draft.dsnEnv}
            placeholder={DSN_ENV_EXAMPLES[draft.profile] ?? "NOME_DA_VARIAVEL"}
            spellCheck={false}
            aria-invalid={envInvalid}
            onChange={(event) => set({ dsnEnv: event.target.value })}
          />
          <p className={`text-xs ${envInvalid ? "text-warning" : "text-muted-foreground"}`}>
            {envInvalid
              ? "Informe só o NOME da variável (letras, números e _), nunca a conexão."
              : "Só o nome da variável. A conexão fica no .env da raiz do projeto e não passa pela interface."}
          </p>
        </div>
      </fieldset>

      {error ? <Notice tone="warning">{error}</Notice> : null}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={!ready || save.isPending}>
          {project ? (
            "Salvar"
          ) : (
            <>
              <Plus className="h-4 w-4" aria-hidden="true" /> Cadastrar o projeto
            </>
          )}
        </Button>
        {project ? (
          <Button variant="ghost" onClick={onDone}>
            Cancelar
          </Button>
        ) : null}
        {project ? <span className="text-xs text-muted-foreground">{projectLabel(project)}</span> : null}
      </div>
    </form>
  );
}
