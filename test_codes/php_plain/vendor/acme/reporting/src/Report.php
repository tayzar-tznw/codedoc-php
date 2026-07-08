<?php
declare(strict_types=1);

namespace Acme\Reporting;

class Report
{
    public function generate(): string
    {
        return 'acme-report';
    }

    public function render(): string
    {
        return strtoupper($this->generate());
    }
}
