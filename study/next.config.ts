import type { NextConfig } from 'next';
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || '/stt_server_cdh/study';
const config: NextConfig = {
  output: 'export',
  basePath,
  trailingSlash: true,
  poweredByHeader: false,
  images: { unoptimized: true },
};
export default config;
