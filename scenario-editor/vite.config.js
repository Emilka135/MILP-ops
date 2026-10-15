import { defineConfig } from 'vite';
export default defineConfig({build:{outDir:'dist'},server:{proxy:{'/api':'http://127.0.0.1:8090','/health':'http://127.0.0.1:8090'} }});
