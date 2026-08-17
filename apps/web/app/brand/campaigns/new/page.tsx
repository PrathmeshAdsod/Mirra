import { BrandCampaignBuilder } from "@/components/brand-campaign-builder";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

export default async function NewCampaignPage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const campaign = Array.isArray(params.campaign) ? params.campaign[0] : params.campaign;
  return <BrandCampaignBuilder initialStep={params.step === "review" && campaign ? 4 : 0} initialCampaignId={campaign} />;
}
