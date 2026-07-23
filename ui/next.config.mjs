/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone", // small Docker image; copies only the needed node_modules
  reactStrictMode: true,
};
export default nextConfig;
