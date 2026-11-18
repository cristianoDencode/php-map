/** Seletor de projeto no topo de todas as telas: nome e perfil, com o atalho para os projetos. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FolderGit2 } from "lucide-react";
import { Link } from "react-router";
import { request } from "../lib/api";
import { useIsAdmin } from "../lib/auth";
import { activeOf, projectLabel, SetActiveProjectDocument, useProjects } from "../lib/projects";

export function ProjectSwitcher() {
  const client = useQueryClient();
  const admin = useIsAdmin();
  const projects = useProjects();
  const switchTo = useMutation({
    mutationFn: (slug: string) => request(SetActiveProjectDocument, { slug }),
    // O servidor passa a servir outro arquivo de grafo: tudo o que estava na tela é do projeto anterior.
    onSuccess: () => void client.invalidateQueries(),
  });
  const list = projects.data?.projects ?? [];
  if (list.length === 0) return null;
  const active = activeOf(projects.data);
  return (
    <div className="flex items-center gap-1.5">
      <FolderGit2 className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
      <select
        aria-label="Projeto ativo"
        value={active?.slug ?? ""}
        disabled={switchTo.isPending || !admin}
        title={admin ? undefined : "Só o administrador troca o projeto ativo"}
        onChange={(event) => switchTo.mutate(event.target.value)}
        className="h-8 max-w-56 rounded-md border border-input bg-card px-2 text-sm"
      >
        {list.map((project) => (
          <option key={project.slug} value={project.slug}>
            {projectLabel(project)}
            {project.hasGraph ? "" : " (sem grafo)"}
          </option>
        ))}
      </select>
      <Link to="/projetos" className="hidden text-xs text-primary hover:underline xl:inline">
        Projetos
      </Link>
    </div>
  );
}
