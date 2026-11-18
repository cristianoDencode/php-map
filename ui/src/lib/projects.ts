/**
 * Projetos: consultas, mutações e funções puras.
 *
 * Um projeto é um sistema mapeado (e-cidade, um projeto zend1) com a pasta, o perfil e o grafo dele. O
 * servidor serve o grafo do projeto ativo; trocar de projeto recarrega as telas. O DSN nunca
 * passa por aqui: o cadastro só tem o NOME da variável do `.env`.
 */
import { useQuery } from "@tanstack/react-query";
import { graphql } from "../graphql/generated";
import type { ProjectsQuery } from "../graphql/generated/graphql";
import { request } from "./api";

export const ProjectsDocument = graphql(`
  query Projects {
    projects {
      slug
      name
      profile
      source
      sourceExists
      hasGraph
      lastScan
      schemaFromDb
      dsnEnv
      dsnAvailable
      active
      scanning
    }
    activeProject {
      slug
    }
  }
`);

export const ProfilesDocument = graphql(`
  query ProjectProfiles {
    projectProfiles {
      name
      marker
    }
  }
`);

export const SuggestProfileDocument = graphql(`
  query SuggestProfile($source: String!) {
    suggestProfile(source: $source) {
      exists
      profile
      markers {
        profile
        marker
        found
      }
    }
  }
`);

export const AddProjectDocument = graphql(`
  mutation AddProject($input: ProjectInput!) {
    addProject(input: $input) {
      slug
      name
    }
  }
`);

export const UpdateProjectDocument = graphql(`
  mutation UpdateProject($slug: String!, $input: ProjectUpdate!) {
    updateProject(slug: $slug, input: $input) {
      slug
      name
    }
  }
`);

export const RemoveProjectDocument = graphql(`
  mutation RemoveProject($slug: String!) {
    removeProject(slug: $slug) {
      slug
    }
  }
`);

export const SetActiveProjectDocument = graphql(`
  mutation SetActiveProject($slug: String!) {
    setActiveProject(slug: $slug) {
      slug
      hasGraph
    }
  }
`);

export type Project = ProjectsQuery["projects"][number];

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: () => request(ProjectsDocument),
    meta: { silent: true }, // o seletor só some se o servidor não responde
  });
}

/** `Zend App · zend1`: o que o seletor mostra. */
export function projectLabel(project: Pick<Project, "name" | "profile">): string {
  return `${project.name} · ${project.profile}`;
}

/** O projeto ativo da resposta de `Projects` (ou `null` sem nenhum). */
export function activeOf(data: ProjectsQuery | undefined): Project | null {
  if (!data) return null;
  const slug = data.activeProject?.slug;
  return data.projects.find((p) => p.slug === slug) ?? null;
}

/** Texto do aviso quando a pasta não é do perfil: a API já diz qual marcador falta. */
export function profileHint(profile: string, marker: string | undefined): string {
  return marker ? `O perfil ${profile} exige ${marker} na raiz da pasta.` : `Perfil ${profile}.`;
}

/**
 * Nome da variável do `.env`: letras, números e `_`. Um valor que parece uma conexão
 * (`postgresql://…`, `usuario:senha@host`) nunca deve ser enviado: o servidor também recusa.
 */
export function isEnvName(value: string): boolean {
  return /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(value.trim());
}

/** Sugestões de nome de variável por perfil (só o nome, nunca o valor). */
export const DSN_ENV_EXAMPLES: Record<string, string> = {
  ecidade: "PHP_MAP_CATALOG_DSN",
  zend1: "SEGUNDO_PROJETO_DSN",
};
