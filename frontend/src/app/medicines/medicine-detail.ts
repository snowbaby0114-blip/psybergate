import { Component, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Subject, distinctUntilChanged, forkJoin, map } from 'rxjs';

import { MedicinesApi } from '../api/medicines-api';
import { RuleSupersedeResult } from '../api/models';
import { ErrorPanel } from '../shared/error-panel';
import { loadLatest } from '../shared/load-state';
import { previousDay } from '../shared/time';
import { NewRuleForm } from './new-rule-form';
import { RuleTimeline } from './rule-timeline';

interface Notice {
  readonly code: string;
  readonly result: RuleSupersedeResult;
}

@Component({
  selector: 'app-medicine-detail',
  imports: [RouterLink, ErrorPanel, NewRuleForm, RuleTimeline],
  templateUrl: './medicine-detail.html',
})
export class MedicineDetail {
  private readonly api = inject(MedicinesApi);
  private readonly route = inject(ActivatedRoute);
  private readonly reloads = new Subject<void>();

  protected readonly view = toSignal(
    loadLatest(
      this.route.paramMap.pipe(
        map((params) => params.get('code') ?? ''),
        distinctUntilChanged(),
      ),
      (code) => forkJoin({ medicine: this.api.get(code), rules: this.api.rules(code) }),
      this.reloads,
    ),
    { requireSync: true },
  );

  /**
   * What the last introduced rule did. It is kept here rather than in the form because reloading the
   * timeline replaces the form; it is only shown for the medicine it was about.
   */
  protected readonly notice = signal<Notice | null>(null);
  protected readonly lastDay = previousDay;

  protected reload(): void {
    this.reloads.next();
  }

  protected introduced(code: string, result: RuleSupersedeResult): void {
    this.notice.set({ code, result });
    this.reload();
  }
}
