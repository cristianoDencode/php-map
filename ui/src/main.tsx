import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import { AppProviders, createQueryClient, routes } from "./app/App";
import "./index.css";

const router = createBrowserRouter(routes);
const client = createQueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
);
