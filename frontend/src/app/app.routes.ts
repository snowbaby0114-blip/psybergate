import { ActivatedRouteSnapshot, Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'medicines' },
  {
    path: 'medicines',
    title: 'Medicines · EMGuidance',
    loadComponent: () => import('./medicines/medicine-search').then((m) => m.MedicineSearch),
  },
  {
    path: 'medicines/:code',
    title: (route: ActivatedRouteSnapshot) =>
      `${route.paramMap.get('code') ?? 'Medicine'} · EMGuidance`,
    loadComponent: () => import('./medicines/medicine-detail').then((m) => m.MedicineDetail),
  },
  {
    path: 'dispense',
    title: 'Dispense · EMGuidance',
    loadComponent: () => import('./dispense/dispense-form').then((m) => m.DispenseForm),
  },
  {
    path: 'ledger',
    title: 'Patient ledger · EMGuidance',
    loadComponent: () => import('./ledger/ledger').then((m) => m.Ledger),
  },
  { path: '**', redirectTo: 'medicines' },
];
