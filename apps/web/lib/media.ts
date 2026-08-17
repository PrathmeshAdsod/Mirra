const base = (process.env.NEXT_PUBLIC_DEMO_MEDIA_BASE_URL || "/demo-media").replace(/\/$/, "");

export function mediaUrl(file: string) {
  return `${base}/${file}`;
}
