import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  poweredByHeader: false,
  generateBuildId: async () => "replayable-dashboard-v1",
};

export default nextConfig;
