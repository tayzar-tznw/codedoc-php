<?php
declare(strict_types=1);

namespace App\S06_Traits;

trait LogsActivity
{
    public function record(string $event): string
    {
        return 'logged:' . $event;
    }

    public function touch(): string
    {
        return $this->record('touch');
    }
}
