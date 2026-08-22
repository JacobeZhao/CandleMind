import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import Sidebar from "./Sidebar";

describe("Sidebar", () => {
  it("uses the project logo for the persistent brand mark", () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );

    const logo = screen.getByRole("img", { name: "CandleMind" });
    expect(logo.getAttribute("src")).toBe("/candlemind-logo.png");
  });
});
