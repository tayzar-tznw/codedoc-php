<?php
declare(strict_types=1);

namespace App\S01_Aliases;

use Acme\Reporting\Report;
use Globex\Reporting\Report as GlobexReport;

class AliasedConsumer
{
    public function buildAcme(): string
    {
        $report = new Report();

        return $report->generate();
    }

    public function buildGlobex(): string
    {
        $report = new GlobexReport();

        return $report->generate();
    }
}
