import { MirrorPlayer } from "@/components/mirror-player";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

function first(value: string | string[] | undefined) {
  return Array.isArray(value) ? value[0] : value;
}

export default async function MirrorPage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  return <MirrorPlayer manifestId={first(params.manifest) || null} initialSessionId={first(params.session) || null} />;
}
