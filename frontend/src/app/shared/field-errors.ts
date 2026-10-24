import { Component, input } from '@angular/core';
import { AbstractControl } from '@angular/forms';

import { fieldMessages, showsErrors } from './violations';

/**
 * Every message for one control, as a list the input points to with aria-describedby. The list element
 * is always present (empty when there is nothing to say) so that reference never dangles.
 */
@Component({
  selector: 'app-field-errors',
  template: `
    <ul class="field-errors" [id]="listId()">
      @if (visible()) {
        @for (message of messages(); track $index) {
          <li>{{ message }}</li>
        }
      }
    </ul>
  `,
})
export class FieldErrors {
  readonly control = input.required<AbstractControl>();
  readonly listId = input.required<string>();

  protected visible(): boolean {
    return showsErrors(this.control());
  }

  protected messages(): string[] {
    return fieldMessages(this.control());
  }
}
